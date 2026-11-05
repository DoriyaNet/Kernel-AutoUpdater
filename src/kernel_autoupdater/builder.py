"""Kernel discovery, download, build and installation pipeline."""

from __future__ import annotations

import bz2
import gzip
import lzma
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from kernel_autoupdater import kernelorg
from kernel_autoupdater.config import Config
from kernel_autoupdater.lock import update_lock
from kernel_autoupdater.kernelorg import (
    Feed,
    KernelRelease,
    ReleaseNotFoundError,
)
from kernel_autoupdater.runner import CommandRunner
from kernel_autoupdater.state import State, StateStore
from kernel_autoupdater.system import (
    boot_image_exists,
    cpu_count,
    find_kernel_config,
    free_gb,
    initramfs_exists,
    is_linux,
    running_kernel,
    secure_boot_enabled,
    write_kernel_config,
)
from kernel_autoupdater.versioning import Channel, KernelVersion, VersionError

LOGGER = logging.getLogger(__name__)


class UpdateError(RuntimeError):
    """Raised when an update cannot be completed safely."""


@dataclass
class CheckResult:
    current: str
    current_base: str
    channel: str
    latest: KernelRelease
    update_available: bool
    feed_stale: bool = False
    installed_release: Optional[str] = None
    pending_reboot: bool = False


@dataclass
class UpdateOptions:
    target: Optional[str] = None
    dry_run: bool = False
    assume_yes: bool = False
    force: bool = False
    reboot: bool = False
    keep_build: bool = False
    defconfig: bool = False
    jobs: Optional[int] = None


@dataclass
class UpdateResult:
    status: str
    release: KernelRelease
    kernel_release: Optional[str] = None


def check_for_update(
    config: Config,
    state: State,
    feed: Optional[Feed] = None,
    current: Optional[str] = None,
) -> CheckResult:
    channel = Channel.parse(config.channel)
    if feed is None:
        feed = kernelorg.fetch_feed(
            config.feed_url,
            cache_path=config.feed_cache_path,
            timeout=config.feed_timeout,
        )
    latest = kernelorg.select_release(feed, channel)
    current_kernel = current or running_kernel()
    try:
        current_version = KernelVersion.parse(current_kernel)
    except VersionError as exc:
        raise UpdateError(
            "cannot parse running kernel release {!r}; this tool is intended "
            "for Linux hosts".format(current_kernel)
        ) from exc

    installed_match = None
    for item in state.installed:
        try:
            if KernelVersion.parse(item.version) == latest.parsed_version:
                installed_match = item
                break
        except VersionError:
            continue
    pending_reboot = bool(
        installed_match
        and latest.parsed_version > current_version
        and boot_image_exists(installed_match.release)
    )
    update_available = latest.parsed_version > current_version and not pending_reboot
    installed_release = state.installed[0].release if state.installed else None
    return CheckResult(
        current=current_kernel,
        current_base=str(current_version),
        channel=str(channel),
        latest=latest,
        update_available=update_available,
        feed_stale=feed.stale,
        installed_release=installed_release,
        pending_reboot=pending_reboot,
    )


class Updater:
    def __init__(
        self,
        config: Config,
        runner: CommandRunner,
        state_store: StateStore,
        current: Optional[str] = None,
        feed_loader: Callable[..., Feed] = kernelorg.fetch_feed,
        downloader: Callable[..., Path] = kernelorg.download,
    ):
        self.config = config
        self.runner = runner
        self.state_store = state_store
        self.current = current or running_kernel()
        self.feed_loader = feed_loader
        self.downloader = downloader
        self.state = state_store.load()

    def _load_feed(self) -> Feed:
        return self.feed_loader(
            self.config.feed_url,
            cache_path=self.config.feed_cache_path,
            timeout=self.config.feed_timeout,
        )

    def _resolve_release(self, feed: Feed, options: UpdateOptions) -> KernelRelease:
        if options.target:
            version = options.target.strip().lstrip("v")
            parsed = KernelVersion.parse(version)
            canonical = str(parsed)
            release = kernelorg.find_release(feed, canonical)
            if release is None:
                try:
                    release = kernelorg.release_for_version(
                        canonical, allow_rc=parsed.rc is not None
                    )
                except (ReleaseNotFoundError, VersionError) as exc:
                    raise UpdateError(str(exc)) from exc
                LOGGER.info(
                    "version %s is not listed in the feed; using %s",
                    canonical,
                    release.source,
                )
            return release
        return kernelorg.select_release(feed, Channel.parse(self.config.channel))

    def execute(
        self,
        options: UpdateOptions,
        release: Optional[KernelRelease] = None,
        feed: Optional[Feed] = None,
    ) -> UpdateResult:
        if release is None:
            feed = feed or self._load_feed()
            release = self._resolve_release(feed, options)

        try:
            current_version = KernelVersion.parse(self.current)
        except VersionError as exc:
            raise UpdateError(
                "cannot parse running kernel release {!r}; this tool is "
                "intended for Linux hosts".format(self.current)
            ) from exc
        target_version = release.parsed_version
        installed_match = self._installed_match(release)
        if (
            installed_match is not None
            and target_version > current_version
            and not options.force
            and boot_image_exists(installed_match.release)
        ):
            LOGGER.info(
                "kernel %s (%s) is already installed; reboot to activate it",
                release.version,
                installed_match.release,
            )
            self.state.record_check(release.version)
            self._save_state()
            return UpdateResult("pending-reboot", release, installed_match.release)

        if target_version <= current_version and not options.force:
            LOGGER.info(
                "kernel is already up to date (running %s, latest %s)",
                self.current,
                release.version,
            )
            self.state.record_check(release.version)
            self._save_state()
            return UpdateResult("up-to-date", release)

        if options.dry_run:
            self._print_plan(release, options)
            return UpdateResult("dry-run", release)

        self._preflight(release, options)
        self._confirm(release, options)

        try:
            with update_lock(self.config.lock_path):
                return self._execute_locked(release, options)
        except Exception as exc:
            self.state.record_error(str(exc))
            self._save_state()
            raise

    def _execute_locked(
        self, release: KernelRelease, options: UpdateOptions
    ) -> UpdateResult:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.config.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.config.build_dir.mkdir(parents=True, exist_ok=True)

        self._step(1, 8, "Download and verify")
        archive = self._download_source(release)

        self._step(2, 8, "Extract source")
        source_dir = self._extract_source(archive, release, options)

        self._step(3, 8, "Apply local patches")
        self._apply_patches(source_dir)

        self._step(4, 8, "Synchronize kernel config")
        self._configure(source_dir, options)

        self._step(5, 8, "Build kernel and modules")
        jobs = options.jobs or self.config.jobs or cpu_count()
        self.runner.run(
            ["make", "-j{}".format(jobs), "all"], cwd=source_dir
        )

        self._step(6, 8, "Install kernel and modules")
        kernel_release = self._kernel_release(source_dir)
        self.runner.run(["make", "modules_install"], cwd=source_dir)
        self.runner.run(["make", "install"], cwd=source_dir)

        self._step(7, 8, "Refresh initramfs and bootloader")
        if self.runner.which("depmod"):
            self.runner.run(["depmod", "-a", kernel_release], check=False)
        self._update_initramfs(kernel_release)
        self._update_bootloader()
        self._verify_install(kernel_release)

        if not (options.keep_build or self.config.keep_build_dir):
            LOGGER.info("removing build tree %s", source_dir)
            shutil.rmtree(str(source_dir), ignore_errors=True)

        self._step(8, 8, "Record state")
        self.state.record_check(release.version)
        self.state.record_install(
            release.version, kernel_release, release.source, self.current
        )
        self._save_state()

        if options.reboot:
            self._schedule_reboot(kernel_release)

        LOGGER.info(
            "kernel %s (%s) installed successfully; running kernel remains %s",
            release.version,
            kernel_release,
            self.current,
        )
        return UpdateResult("installed", release, kernel_release)

    def _download_source(self, release: KernelRelease) -> Path:
        filename = posixpath.basename(urlparse(release.source).path)
        if not filename:
            raise UpdateError("source URL has no filename: {}".format(release.source))

        checksum_url = release.checksums_url
        expected = None
        if checksum_url:
            expected = self._fetch_checksum(checksum_url, filename, release.version)
        elif not self.config.allow_unverified:
            raise UpdateError(
                "no SHA-256 checksum is available for {}; refusing to build an "
                "unverified tarball. Set allow_unverified = true only if you "
                "accept that risk.".format(release.source)
            )
        else:
            LOGGER.warning(
                "no checksum available for %s; continuing because "
                "allow_unverified is enabled",
                release.source,
            )

        archive = self.config.downloads_dir / filename
        archive = self.downloader(
            release.source,
            archive,
            expected_sha256=expected,
            timeout=self.config.download_timeout,
        )
        if self.config.verify_pgp:
            self._verify_tarball_signature(release, archive)
        return archive

    def _fetch_checksum(self, url: str, filename: str, version: str) -> str:
        major = KernelVersion.parse(version).major
        checksum_file = self.config.downloads_dir / "sha256sums-v{}.x.asc".format(major)
        if checksum_file.exists():
            checksum_file.unlink()
        self.downloader(url, checksum_file, timeout=self.config.download_timeout)

        if not self.config.verify_pgp:
            LOGGER.warning(
                "PGP verification is disabled; trusting HTTPS plus the SHA-256 "
                "checksum. Enable source.verify_pgp to verify the kernel.org "
                ".tar.sign signature."
            )

        text = checksum_file.read_text(encoding="utf-8", errors="replace")
        checksums = kernelorg.parse_sha256sums(text)
        expected = checksums.get(filename)
        if not expected:
            raise UpdateError(
                "{} does not list {}".format(url, filename)
            )
        return expected

    def _verify_tarball_signature(
        self, release: KernelRelease, archive: Path
    ) -> None:
        gpg = self.runner.which("gpg")
        if not gpg:
            raise UpdateError(
                "source.verify_pgp is enabled but gpg is not installed"
            )
        keyring = self.config.pgp_keyring
        if not keyring.is_file():
            raise UpdateError(
                "source.verify_pgp is enabled but the keyring does not exist: {}"
                .format(keyring)
            )
        if not release.pgp:
            raise UpdateError(
                "release {} has no PGP signature; use a stable/longterm release "
                "or disable source.verify_pgp".format(release.version)
            )

        signature_file = self.config.downloads_dir / posixpath.basename(
            urlparse(release.pgp).path
        )
        if signature_file.exists():
            signature_file.unlink()
        self.downloader(
            release.pgp, signature_file, timeout=self.config.download_timeout
        )
        LOGGER.info(
            "verifying PGP signature %s (streaming decompressed source)",
            signature_file.name,
        )

        opener = self._compressed_opener(archive)
        process = subprocess.Popen(
            [
                gpg,
                "--batch",
                "--no-tty",
                "--no-default-keyring",
                "--keyring",
                str(keyring),
                "--verify",
                str(signature_file),
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            with opener(str(archive), "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    try:
                        process.stdin.write(chunk)
                    except BrokenPipeError:
                        break
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
            raise
        finally:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except BrokenPipeError:
                pass

        if process.stderr is not None:
            stderr = process.stderr.read()
            process.stderr.close()
        else:
            stderr = b""
        returncode = process.wait()
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise UpdateError(
                "PGP signature verification failed for {}: {}".format(
                    release.version, detail or "gpg exited with {}".format(returncode)
                )
            )
        LOGGER.info("PGP signature verified for %s", release.version)

    @staticmethod
    def _compressed_opener(archive: Path):
        name = str(archive).lower()
        if name.endswith(".xz"):
            return lzma.open
        if name.endswith(".gz"):
            return gzip.open
        if name.endswith(".bz2"):
            return bz2.open
        if name.endswith(".tar"):
            return open
        raise UpdateError(
            "cannot verify signature for unsupported archive type: {}".format(archive)
        )

    def _extract_source(
        self, archive: Path, release: KernelRelease, options: UpdateOptions
    ) -> Path:
        LOGGER.debug("preparing source tree for %s", release.version)
        source_dir = self.config.build_dir / self._archive_top_dir(archive)
        if source_dir.exists():
            if options.keep_build:
                LOGGER.info("reusing existing build directory %s", source_dir)
                if not (source_dir / "Makefile").exists():
                    raise UpdateError(
                        "build directory is incomplete: {}".format(source_dir)
                    )
                return source_dir
            LOGGER.info("removing previous build directory %s", source_dir)
            shutil.rmtree(str(source_dir))

        self._safe_extract(archive, self.config.build_dir)
        if not source_dir.is_dir():
            raise UpdateError(
                "archive did not create expected directory {}".format(source_dir)
            )
        if not (source_dir / "Makefile").exists():
            raise UpdateError("{} does not look like a kernel source tree".format(source_dir))
        return source_dir

    @staticmethod
    def _archive_top_dir(archive: Path) -> str:
        try:
            with tarfile.open(str(archive), "r:*") as tar:
                fallback = None
                for member in tar:
                    top = member.name.split("/", 1)[0]
                    if top and top not in (".", "..") and "/" not in top:
                        if fallback is None:
                            fallback = top
                        if top.startswith("linux-"):
                            return top
                if fallback:
                    return fallback
        except (OSError, tarfile.TarError) as exc:
            raise UpdateError("cannot read archive {}: {}".format(archive, exc)) from exc
        raise UpdateError("archive {} is empty".format(archive))

    @staticmethod
    def _safe_extract(archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        base = destination.resolve()
        with tarfile.open(str(archive), "r:*") as tar:
            members = tar.getmembers()
            for member in members:
                target = (destination / member.name).resolve()
                if base not in target.parents and target != base:
                    raise UpdateError(
                        "refusing to extract path outside build directory: {}"
                        .format(member.name)
                    )
                if member.issym() or member.islnk():
                    link_base = target.parent if member.issym() else base
                    link_target = (link_base / member.linkname).resolve()
                    if base not in link_target.parents and link_target != base:
                        raise UpdateError(
                            "refusing to extract unsafe link: {}".format(member.name)
                        )
            if hasattr(tarfile, "data_filter"):
                tar.extractall(str(destination), filter="data")
            else:  # pragma: no cover - Python < 3.12
                tar.extractall(str(destination))

    def _apply_patches(self, source_dir: Path) -> None:
        patch_dir = self.config.patches_dir
        if not patch_dir.is_dir():
            LOGGER.debug("patch directory %s does not exist", patch_dir)
            return
        patches = sorted(
            list(patch_dir.glob("*.patch")) + list(patch_dir.glob("*.diff"))
        )
        if not patches:
            LOGGER.info("no local patches found in %s", patch_dir)
            return
        marker = source_dir / ".kau-patches-applied"
        if marker.exists():
            LOGGER.info("local patches were already applied to this build tree")
            return
        if not self.runner.which("patch"):
            raise UpdateError("local patches exist but the patch command is missing")
        for patch in patches:
            LOGGER.info("applying %s", patch.name)
            self.runner.run(
                [
                    "patch",
                    "-p1",
                    "--batch",
                    "--forward",
                    "--no-backup-if-mismatch",
                    "-i",
                    str(patch),
                ],
                cwd=source_dir,
            )
        marker.write_text("applied\n", encoding="utf-8")

    def _configure(self, source_dir: Path, options: UpdateOptions) -> None:
        spec = "defconfig" if options.defconfig else self.config.config_source
        if spec == "defconfig":
            LOGGER.info("using make defconfig; this may omit drivers for this host")
            self.runner.run(["make", "defconfig"], cwd=source_dir)
        else:
            config_file = source_dir / ".config"
            source = write_kernel_config(spec, self.current, config_file)
            LOGGER.info("using kernel config from %s", source)
            self.runner.run(["make", "olddefconfig"], cwd=source_dir)

        scripts_config = source_dir / "scripts" / "config"
        if not scripts_config.exists():
            raise UpdateError("kernel source is missing scripts/config")
        self.runner.run(
            [
                "/bin/sh",
                str(scripts_config),
                "--set-str",
                "LOCALVERSION",
                self.config.localversion,
            ],
            cwd=source_dir,
        )
        self.runner.run(
            ["/bin/sh", str(scripts_config), "--disable", "LOCALVERSION_AUTO"],
            cwd=source_dir,
        )
        if self.config.clear_distro_keys:
            for option in ("SYSTEM_TRUSTED_KEYS", "SYSTEM_REVOCATION_KEYS"):
                self.runner.run(
                    ["/bin/sh", str(scripts_config), "--set-str", option, ""],
                    cwd=source_dir,
                )
        self.runner.run(["make", "olddefconfig"], cwd=source_dir)

    def _kernel_release(self, source_dir: Path) -> str:
        result = self.runner.run(
            ["make", "-s", "kernelrelease"], cwd=source_dir, capture=True
        )
        kernel_release = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if not kernel_release or re.search(r"\s", kernel_release):
            raise UpdateError(
                "could not determine the built kernel release from make kernelrelease"
            )
        return kernel_release

    def _update_initramfs(self, kernel_release: str) -> None:
        if not self.config.update_initramfs:
            LOGGER.info("initramfs refresh disabled by configuration")
            return
        if self.runner.which("update-initramfs"):
            mode = "-u" if initramfs_exists(kernel_release) else "-c"
            self.runner.run(["update-initramfs", mode, "-k", kernel_release])
            return
        if self.runner.which("dracut"):
            if initramfs_exists(kernel_release):
                LOGGER.info(
                    "initramfs for %s already exists (created by installkernel)",
                    kernel_release,
                )
                return
            image = Path("/boot/initramfs-{}.img".format(kernel_release))
            self.runner.run(["dracut", "--force", str(image), kernel_release])
            return
        if self.runner.which("mkinitcpio"):
            LOGGER.warning(
                "mkinitcpio detected but no preset was created for %s; add a "
                "preset (or run mkinitcpio with the right config) before rebooting",
                kernel_release,
            )
            return
        LOGGER.warning(
            "no supported initramfs tool found; verify /boot before rebooting"
        )

    def _update_bootloader(self) -> None:
        if not self.config.update_bootloader:
            LOGGER.info("bootloader refresh disabled by configuration")
            return
        if self.runner.which("update-grub"):
            self.runner.run(["update-grub"])
            return
        if (
            self.runner.which("grub2-mkconfig")
            and Path("/boot/grub2").is_dir()
        ):
            self.runner.run(
                ["grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"]
            )
            return
        if self.runner.which("grub-mkconfig") and Path("/boot/grub").is_dir():
            self.runner.run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
            return
        if self.runner.which("bootctl"):
            LOGGER.info(
                "systemd-boot detected; kernel-install should have created a "
                "boot entry automatically"
            )
            return
        LOGGER.warning(
            "no supported bootloader update command found; add the new kernel "
            "to the bootloader manually before rebooting"
        )

    def _verify_install(self, kernel_release: str) -> None:
        modules = Path("/lib/modules") / kernel_release
        if not modules.is_dir():
            modules = Path("/usr/lib/modules") / kernel_release
        if not modules.is_dir():
            if boot_image_exists(kernel_release):
                LOGGER.warning(
                    "no module directory for %s; this is expected for a "
                    "monolithic kernel",
                    kernel_release,
                )
            else:
                raise UpdateError(
                    "modules were not installed under /lib/modules/{}".format(
                        kernel_release
                    )
                )
        if not boot_image_exists(kernel_release):
            LOGGER.warning(
                "could not find a boot image for %s under /boot; check that "
                "installkernel/kernel-install created a boot entry",
                kernel_release,
            )
        elif self.runner.which("bootctl"):
            entries = list(Path("/boot/loader/entries").glob("*{}*.conf".format(kernel_release)))
            if entries:
                LOGGER.info("systemd-boot entry: %s", entries[0].name)
        if modules.is_dir():
            LOGGER.info("installed modules at %s", modules)

    def _schedule_reboot(self, kernel_release: str) -> None:
        delay = self.config.reboot_delay_minutes
        if delay > 0:
            command = [
                "shutdown",
                "-r",
                "+{}".format(delay),
                "kernel-autoupdater: reboot to activate {}".format(kernel_release),
            ]
            LOGGER.info("scheduling reboot in %d minute(s)", delay)
        else:
            command = ["shutdown", "-r", "now"]
            LOGGER.info("rebooting now")
        self.runner.run(command)

    def _preflight(self, release: KernelRelease, options: UpdateOptions) -> None:
        if not is_linux():
            raise UpdateError("kernel updates are only supported on Linux")
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and geteuid() != 0:
            raise UpdateError("kernel updates must run as root (try sudo)")

        if secure_boot_enabled(self.runner) is True and not options.force:
            raise UpdateError(
                "UEFI Secure Boot is enabled and this tool installs an unsigned "
                "kernel. Sign the kernel or disable Secure Boot, then rerun with "
                "--force if you understand the risk."
            )

        if not release.checksums_url and not self.config.allow_unverified:
            raise UpdateError(
                "{} has no published SHA-256 checksum. Use a stable/longterm "
                "release, or set allow_unverified = true if you accept the "
                "risk.".format(release.version)
            )
        if self.config.verify_pgp and not self.config.pgp_keyring.is_file():
            raise UpdateError(
                "source.verify_pgp is enabled but the keyring does not exist: {}"
                .format(self.config.pgp_keyring)
            )
        if self.config.verify_pgp and not release.pgp:
            raise UpdateError(
                "source.verify_pgp is enabled but {} has no .tar.sign "
                "signature".format(release.version)
            )

        required = ["make", "bc", "flex", "bison", "perl", "tar"]
        missing = [command for command in required if not self.runner.which(command)]
        if not self.runner.which("gcc") and not self.runner.which("cc"):
            missing.append("gcc/cc")
        if self._patches_available() and not self.runner.which("patch"):
            missing.append("patch")
        if self.config.verify_pgp and not self.runner.which("gpg"):
            missing.append("gpg")
        if missing:
            raise UpdateError(
                "missing build dependency/binaries: {}. Install your distribution's "
                "kernel build toolchain first (gcc, make, bc, flex, bison, perl, "
                "libssl-dev/openssl-devel, dwarves/pahole, cpio).".format(
                    ", ".join(missing)
                )
            )
        if self.config.update_initramfs and not any(
            self.runner.which(tool)
            for tool in ("update-initramfs", "dracut", "mkinitcpio")
        ):
            LOGGER.warning(
                "no supported initramfs tool found; the new kernel may not boot"
            )
        for optional in ("pahole", "cpio"):
            if not self.runner.which(optional):
                LOGGER.warning(
                    "%s is missing; BTF or initramfs generation may fail",
                    optional,
                )

        spec = "defconfig" if options.defconfig else self.config.config_source
        find_kernel_config(spec, self.current)

        available = free_gb(self.config.work_dir)
        if available < self.config.min_free_gb:
            raise UpdateError(
                "only {:.1f} GiB free under {}; at least {} GiB is required "
                "(updater.min_free_gb)".format(
                    available, self.config.work_dir, self.config.min_free_gb
                )
            )

        boot = Path("/boot")
        if boot.is_dir():
            boot_free = free_gb(boot)
            if boot_free < 0.5:
                LOGGER.warning(
                    "/boot has only %.0f MiB free; installation may fail",
                    boot_free * 1024,
                )

        LOGGER.info(
            "preflight OK: updating %s -> %s (source: %s)",
            self.current,
            release.version,
            release.moniker,
        )

    def _patches_available(self) -> bool:
        directory = self.config.patches_dir
        if not directory.is_dir():
            return False
        return bool(list(directory.glob("*.patch")) or list(directory.glob("*.diff")))

    def _installed_match(self, release: KernelRelease):
        for item in self.state.installed:
            try:
                if KernelVersion.parse(item.version) == release.parsed_version:
                    return item
            except VersionError:
                continue
        return None

    def _confirm(self, release: KernelRelease, options: UpdateOptions) -> None:
        if options.assume_yes:
            return
        jobs = options.jobs or self.config.jobs or cpu_count()
        LOGGER.info("current kernel : %s", self.current)
        LOGGER.info("target kernel  : %s (%s)", release.version, release.moniker)
        LOGGER.info("source         : %s", release.source)
        LOGGER.info("build jobs     : %s", jobs)
        LOGGER.info(
            "reboot         : %s",
            "yes" if options.reboot else "no",
        )
        if not sys.stdin.isatty():
            raise UpdateError("confirmation required; rerun with --yes")
        try:
            answer = input("Build and install this kernel? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            raise UpdateError("aborted by user")

    def _print_plan(self, release: KernelRelease, options: UpdateOptions) -> None:
        jobs = options.jobs or self.config.jobs or cpu_count()
        spec = "defconfig" if options.defconfig else self.config.config_source
        LOGGER.info("dry-run plan")
        LOGGER.info("  current kernel : %s", self.current)
        LOGGER.info("  target kernel  : %s (%s)", release.version, release.moniker)
        LOGGER.info("  source         : %s", release.source)
        if release.checksums_url:
            LOGGER.info("  checksums      : %s", release.checksums_url)
        else:
            LOGGER.info("  checksums      : unavailable")
        LOGGER.info("  config source  : %s", spec)
        LOGGER.info("  local version  : %r", self.config.localversion)
        LOGGER.info("  build dir      : %s/linux-*/", self.config.build_dir)
        LOGGER.info("  patches dir    : %s", self.config.patches_dir)
        LOGGER.info("  make           : make -j%d all", jobs)
        LOGGER.info("  install        : make modules_install && make install")
        LOGGER.info(
            "  cleanup        : %s",
            "keep build tree"
            if (options.keep_build or self.config.keep_build_dir)
            else "remove build tree after success",
        )
        LOGGER.info("  reboot         : %s", "yes" if options.reboot else "no")

    @staticmethod
    def _step(index: int, total: int, title: str) -> None:
        LOGGER.info("[%d/%d] %s", index, total, title)

    def _save_state(self) -> None:
        try:
            self.state_store.save(self.state)
        except OSError as exc:
            LOGGER.warning("cannot save updater state: %s", exc)

