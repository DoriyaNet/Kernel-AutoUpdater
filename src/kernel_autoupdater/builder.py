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
from kernel_autoupdater import packages as distro_packages
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
    installed_module_releases,
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
    method: str = "build"
    package: Optional[distro_packages.PackageUpdate] = None

    @property
    def latest_version(self) -> str:
        if self.package is not None:
            return self.package.candidate_version
        return self.latest.version


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
            "无法解析当前内核版本 {!r}；此工具只能用于 Linux 主机".format(
                current_kernel
            )
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
        method="build",
    )


def check_package_update(
    config: Config,
    state: State,
    runner: Optional[CommandRunner] = None,
    current: Optional[str] = None,
    backend: Optional[distro_packages.PackageBackend] = None,
    refresh: bool = False,
    installed_releases: Optional[Callable[[], list]] = None,
) -> CheckResult:
    """Check a distro repository for a prebuilt kernel update."""

    runner = runner or CommandRunner()
    current_kernel = current or running_kernel()
    backend = backend or distro_packages.build_backend(
        config, runner, running_release=current_kernel
    )
    try:
        current_version = KernelVersion.parse(current_kernel)
    except VersionError as exc:
        raise UpdateError(
            "无法解析当前内核版本 {!r}；此工具只能用于 Linux 主机".format(
                current_kernel
            )
        ) from exc

    if refresh and not runner.dry_run:
        backend.refresh()
    package_update = backend.inspect(current_kernel)

    release = KernelRelease(
        version=package_update.kernel_version,
        moniker=package_update.manager,
        source=package_update.description,
    )
    pending_reboot = package_update.pending_reboot
    update_available = package_update.update_available and not pending_reboot

    installed_release = None
    if pending_reboot:
        list_releases = installed_releases or installed_module_releases
        installed_release = _installed_release_for_package(
            package_update, list_releases()
        ) or package_update.installed_version
    elif state.installed:
        installed_release = state.installed[0].release

    return CheckResult(
        current=current_kernel,
        current_base=str(current_version),
        channel="package:{}".format(package_update.manager),
        latest=release,
        update_available=update_available,
        installed_release=installed_release,
        pending_reboot=pending_reboot,
        method="package",
        package=package_update,
    )


def _matching_installed_release(
    kernel_version: str, releases: Optional[list] = None
) -> Optional[str]:
    """Pick the newest installed module directory for a kernel base."""

    try:
        wanted = KernelVersion.parse(kernel_version)
    except VersionError:
        return None
    matches = []
    for release in releases if releases is not None else installed_module_releases():
        try:
            if KernelVersion.parse(release).base == wanted.base:
                matches.append(release)
        except VersionError:
            continue
    if not matches:
        return None
    return max(
        matches,
        key=lambda value: (KernelVersion.parse(value).sort_key, value),
    )


def _package_release_prefix(
    update: distro_packages.PackageUpdate,
) -> Optional[str]:
    """Map a package version to the matching /lib/modules prefix.

    ``6.8.0-80.80`` matches ``6.8.0-80-generic`` and
    ``6.11.4-301.fc41`` matches ``6.11.4-301.fc41.x86_64``.
    """

    base = update.kernel_version
    candidate = update.candidate_version.split(":", 1)[-1]
    if not candidate.startswith(base):
        return None
    remainder = candidate[len(base):]
    if not remainder:
        return base
    if remainder.startswith("-"):
        abi = remainder[1:].split(".", 1)[0]
        return base + "-" + abi
    if remainder.startswith("."):
        parts = [part for part in remainder.split(".") if part]
        if parts:
            return base + "-" + parts[0]
    return base


def _installed_release_for_package(
    update: distro_packages.PackageUpdate,
    releases: Optional[list] = None,
) -> Optional[str]:
    available = list(
        releases if releases is not None else installed_module_releases()
    )
    prefix = _package_release_prefix(update)
    if prefix:
        matching = [
            release
            for release in available
            if release == prefix
            or release.startswith(prefix + "-")
            or release.startswith(prefix + ".")
        ]
        if matching:
            parsed = []
            for release in matching:
                try:
                    parsed.append(
                        (KernelVersion.parse(release).sort_key, release)
                    )
                except VersionError:
                    continue
            if parsed:
                return max(parsed)[1]
    return _matching_installed_release(update.kernel_version, available)


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
                    "版本 %s 不在发布信息中，改用 %s",
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
        if self.config.update_method == "package":
            return self._execute_package(options)

        if release is None:
            feed = feed or self._load_feed()
            release = self._resolve_release(feed, options)

        try:
            current_version = KernelVersion.parse(self.current)
        except VersionError as exc:
            raise UpdateError(
                "无法解析当前内核版本 {!r}；此工具只能用于 Linux 主机".format(
                    self.current
                )
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
                "内核 %s (%s) 已安装，请重启以启用",
                release.version,
                installed_match.release,
            )
            self.state.record_check(release.version)
            self._save_state()
            return UpdateResult("pending-reboot", release, installed_match.release)

        if target_version <= current_version and not options.force:
            LOGGER.info(
                "内核已是最新版本（运行 %s，最新 %s）",
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

    def _execute_package(self, options: UpdateOptions) -> UpdateResult:
        incompatible = []
        if options.target:
            incompatible.append("--to")
        if options.defconfig:
            incompatible.append("--defconfig")
        if options.keep_build:
            incompatible.append("--keep-build")
        if options.jobs is not None:
            incompatible.append("--jobs")
        if incompatible:
            raise UpdateError(
                "{} only apply to source builds; remove them or use "
                "update_method = build".format(", ".join(incompatible))
            )

        try:
            current_version = KernelVersion.parse(self.current)
        except VersionError as exc:
            raise UpdateError(
                "无法解析当前内核版本 {!r}；此工具只能用于 Linux 主机".format(
                    self.current
                )
            ) from exc

        backend = distro_packages.build_backend(
            self.config, self.runner, running_release=self.current
        )

        if options.dry_run:
            update = backend.inspect(self.current)
            release = self._package_release(update)
            self._print_package_plan(backend, update, options)
            return UpdateResult("dry-run", release)

        self._preflight_package()

        if self.config.refresh_package_index:
            backend.refresh()
        update = backend.inspect(self.current)
        release = self._package_release(update)
        LOGGER.info(
            "预检通过：软件包 %s -> %s（%s，运行内核 %s）",
            update.installed_version or "none",
            update.candidate_version,
            backend.describe(),
            current_version,
        )

        if update.pending_reboot and not options.force:
            installed_release = _installed_release_for_package(
                update
            ) or update.installed_version
            LOGGER.info(
                "内核包 %s 已安装，请重启以启用",
                update.candidate_version,
            )
            self.state.record_check(update.candidate_version)
            self._save_state()
            return UpdateResult("pending-reboot", release, installed_release)

        if not update.update_available and not options.force:
            LOGGER.info(
                "内核包已是最新版本（已安装 %s，候选 %s）",
                update.installed_version or "none",
                update.candidate_version,
            )
            self.state.record_check(update.candidate_version)
            self._save_state()
            return UpdateResult("up-to-date", release)

        self._confirm_package(backend, update, options)

        try:
            with update_lock(self.config.lock_path):
                return self._execute_package_locked(
                    backend, update, options
                )
        except Exception as exc:
            self.state.record_error(str(exc))
            self._save_state()
            raise

    def _execute_package_locked(
        self,
        backend: distro_packages.PackageBackend,
        update: distro_packages.PackageUpdate,
        options: UpdateOptions,
    ) -> UpdateResult:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        before = set(installed_module_releases())

        self._step(1, 4, "下载并安装预编译内核包")
        backend.install()
        kernel_release = self._detect_package_release(
            update, before, set(installed_module_releases())
        )

        self._step(2, 4, "验证内核安装")
        self._verify_install(kernel_release)

        self._step(3, 4, "刷新 initramfs 和引导项")
        if self.config.update_initramfs or self.config.update_bootloader:
            LOGGER.info(
                "%s 软件包的安装后脚本会处理 initramfs 和引导项更新",
                backend.name,
            )

        self._step(4, 4, "记录状态")
        self.state.record_check(update.candidate_version)
        self.state.record_install(
            update.candidate_version,
            kernel_release,
            update.description,
            self.current,
        )
        self._save_state()

        if options.reboot:
            self._schedule_reboot(kernel_release)

        LOGGER.info(
            "内核包 %s 已安装（%s）；当前运行内核仍为 %s",
            update.candidate_version,
            kernel_release,
            self.current,
        )
        return UpdateResult(
            "installed", self._package_release(update), kernel_release
        )

    @staticmethod
    def _package_release(update: distro_packages.PackageUpdate) -> KernelRelease:
        return KernelRelease(
            version=update.kernel_version,
            moniker=update.manager,
            source=update.description,
        )

    @staticmethod
    def _detect_package_release(
        update: distro_packages.PackageUpdate,
        before: set,
        after: set,
    ) -> str:
        candidates = after - before
        if not candidates:
            prefix = _package_release_prefix(update)
            if prefix:
                matching = {
                    release
                    for release in after
                    if release == prefix
                    or release.startswith(prefix + "-")
                    or release.startswith(prefix + ".")
                }
                if matching:
                    candidates = matching
        if not candidates:
            matched = _matching_installed_release(
                update.kernel_version, list(after)
            )
            if matched:
                candidates = {matched}
        if not candidates:
            candidates = after

        parsed = []
        for release in candidates:
            try:
                parsed.append((KernelVersion.parse(release).sort_key, release))
            except VersionError:
                continue
        if parsed:
            return max(parsed)[1]

        LOGGER.warning(
            "无法从 /lib/modules 判断已安装的内核版本；"
            "改为记录软件包版本 %s",
            update.candidate_version,
        )
        return update.kernel_version

    def _preflight_package(self) -> None:
        if not is_linux():
            raise UpdateError("仅支持在 Linux 上更新内核")
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and geteuid() != 0:
            raise UpdateError("更新内核需要 root 权限，请使用 sudo 运行")

        available = free_gb(self.config.work_dir)
        if available < self.config.package_min_free_gb:
            raise UpdateError(
                "{} 只剩 {:.1f} GiB，至少需要 {} GiB "
                "（packages.min_free_gb）".format(
                    self.config.work_dir,
                    available,
                    self.config.package_min_free_gb,
                )
            )
        boot = Path("/boot")
        if boot.is_dir():
            boot_free = free_gb(boot)
            if boot_free < 0.5:
                LOGGER.warning(
                    "/boot 只剩 %.0f MiB，软件包安装可能失败",
                    boot_free * 1024,
                )
        else:
            LOGGER.warning(
                "/boot 不存在；软件包可能安装成功，但不会生成可启动引导项"
            )
        if secure_boot_enabled(self.runner) is True:
            LOGGER.info(
                "Secure Boot 已启用；发行版内核包通常带签名，"
                "package 模式不编译未签名内核"
            )

    def _confirm_package(
        self,
        backend: distro_packages.PackageBackend,
        update: distro_packages.PackageUpdate,
        options: UpdateOptions,
    ) -> None:
        if options.assume_yes:
            return
        LOGGER.info("当前内核 : %s", self.current)
        LOGGER.info("更新方式 : 预编译包 (%s)", backend.name)
        LOGGER.info("内核包   : %s", ", ".join(backend.packages))
        LOGGER.info("已安装   : %s", update.installed_version or "无")
        LOGGER.info("候选版本 : %s", update.candidate_version)
        LOGGER.info("内核基线 : %s", update.kernel_version)
        LOGGER.info("本地编译 : 跳过")
        LOGGER.info("安装后重启: %s", "是" if options.reboot else "否")
        if not sys.stdin.isatty():
            raise UpdateError("需要确认；请重新运行并加 --yes")
        try:
            answer = input("安装这些内核包？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            raise UpdateError("已由用户取消")

    def _print_package_plan(
        self,
        backend: distro_packages.PackageBackend,
        update: distro_packages.PackageUpdate,
        options: UpdateOptions,
    ) -> None:
        LOGGER.info("试运行计划")
        LOGGER.info("  当前内核 : %s", self.current)
        LOGGER.info("  更新方式 : package (%s)", backend.name)
        LOGGER.info("  内核包   : %s", ", ".join(backend.packages))
        LOGGER.info("  已安装   : %s", update.installed_version or "无")
        LOGGER.info("  候选版本 : %s", update.candidate_version)
        LOGGER.info("  内核基线 : %s", update.kernel_version)
        LOGGER.info(
            "  刷新索引 : %s",
            "是" if self.config.refresh_package_index else "否",
        )
        LOGGER.info("  安装命令 : %s", " ".join(backend.install_command()))
        LOGGER.info("  源码编译 : 跳过")
        LOGGER.info("  配置/补丁: package 模式不使用")
        LOGGER.info("  安装后重启: %s", "是" if options.reboot else "否")

    def _execute_locked(
        self, release: KernelRelease, options: UpdateOptions
    ) -> UpdateResult:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.config.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.config.build_dir.mkdir(parents=True, exist_ok=True)

        self._step(1, 8, "下载并校验源码")
        archive = self._download_source(release)

        self._step(2, 8, "解压源码")
        source_dir = self._extract_source(archive, release, options)

        self._step(3, 8, "应用本地补丁")
        self._apply_patches(source_dir)

        self._step(4, 8, "同步内核配置")
        self._configure(source_dir, options)

        self._step(5, 8, "编译内核和模块")
        jobs = options.jobs or self.config.jobs or cpu_count()
        self.runner.run(
            ["make", "-j{}".format(jobs), "all"], cwd=source_dir
        )

        self._step(6, 8, "安装内核和模块")
        kernel_release = self._kernel_release(source_dir)
        self.runner.run(["make", "modules_install"], cwd=source_dir)
        self.runner.run(["make", "install"], cwd=source_dir)

        self._step(7, 8, "刷新 initramfs 和引导项")
        if self.runner.which("depmod"):
            self.runner.run(["depmod", "-a", kernel_release], check=False)
        self._update_initramfs(kernel_release)
        self._update_bootloader()
        self._verify_install(kernel_release)

        if not (options.keep_build or self.config.keep_build_dir):
            LOGGER.info("正在删除编译目录 %s", source_dir)
            shutil.rmtree(str(source_dir), ignore_errors=True)

        self._step(8, 8, "记录状态")
        self.state.record_check(release.version)
        self.state.record_install(
            release.version, kernel_release, release.source, self.current
        )
        self._save_state()

        if options.reboot:
            self._schedule_reboot(kernel_release)

        LOGGER.info(
            "内核 %s (%s) 安装成功；当前运行内核仍为 %s",
            release.version,
            kernel_release,
            self.current,
        )
        return UpdateResult("installed", release, kernel_release)

    def _download_source(self, release: KernelRelease) -> Path:
        filename = posixpath.basename(urlparse(release.source).path)
        if not filename:
            raise UpdateError("源码地址中没有文件名: {}".format(release.source))

        checksum_url = release.checksums_url
        expected = None
        if checksum_url:
            expected = self._fetch_checksum(checksum_url, filename, release.version)
        elif not self.config.allow_unverified:
            raise UpdateError(
                "{} 没有可用的 SHA-256 校验和，已拒绝编译未校验的源码包。"
                "只有在你接受风险时才应设置 allow_unverified = true。".format(
                    release.source
                )
            )
        else:
            LOGGER.warning(
                "%s 没有可用校验和；因已启用 allow_unverified，继续执行",
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
                "未启用 PGP 校验，仅信任 HTTPS 和 SHA-256 校验和。"
                "如需验证 kernel.org 的 .tar.sign 签名，请启用 "
                "source.verify_pgp。"
            )

        text = checksum_file.read_text(encoding="utf-8", errors="replace")
        checksums = kernelorg.parse_sha256sums(text)
        expected = checksums.get(filename)
        if not expected:
            raise UpdateError(
                "{} 中没有列出 {}".format(url, filename)
            )
        return expected

    def _verify_tarball_signature(
        self, release: KernelRelease, archive: Path
    ) -> None:
        gpg = self.runner.which("gpg")
        if not gpg:
            raise UpdateError(
                "已启用 source.verify_pgp，但系统没有安装 gpg"
            )
        keyring = self.config.pgp_keyring
        if not keyring.is_file():
            raise UpdateError(
                "已启用 source.verify_pgp，但公钥环不存在: {}".format(keyring)
            )
        if not release.pgp:
            raise UpdateError(
                "版本 {} 没有 PGP 签名；请使用 stable/longterm 版本，"
                "或关闭 source.verify_pgp".format(release.version)
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
            "正在校验 PGP 签名 %s（流式读取解压后的源码）",
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
                "版本 {} 的 PGP 签名校验失败: {}".format(
                    release.version,
                    detail or "gpg 退出码 {}".format(returncode),
                )
            )
        LOGGER.info("版本 %s 的 PGP 签名校验通过", release.version)

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
            "无法校验不支持的源码包格式: {}".format(archive)
        )

    def _extract_source(
        self, archive: Path, release: KernelRelease, options: UpdateOptions
    ) -> Path:
        LOGGER.debug("正在准备 %s 的源码目录", release.version)
        source_dir = self.config.build_dir / self._archive_top_dir(archive)
        if source_dir.exists():
            if options.keep_build:
                LOGGER.info("复用已有编译目录 %s", source_dir)
                if not (source_dir / "Makefile").exists():
                    raise UpdateError(
                        "编译目录不完整: {}".format(source_dir)
                    )
                return source_dir
            LOGGER.info("正在删除旧编译目录 %s", source_dir)
            shutil.rmtree(str(source_dir))

        self._safe_extract(archive, self.config.build_dir)
        if not source_dir.is_dir():
            raise UpdateError(
                "源码包解压后没有生成预期目录 {}".format(source_dir)
            )
        if not (source_dir / "Makefile").exists():
            raise UpdateError("{} 不像内核源码目录".format(source_dir))
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
            raise UpdateError("无法读取源码包 {}: {}".format(archive, exc)) from exc
        raise UpdateError("源码包 {} 是空的".format(archive))

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
                        "拒绝解压到编译目录之外的路径: {}".format(member.name)
                    )
                if member.issym() or member.islnk():
                    link_base = target.parent if member.issym() else base
                    link_target = (link_base / member.linkname).resolve()
                    if base not in link_target.parents and link_target != base:
                        raise UpdateError(
                            "拒绝解压不安全的链接: {}".format(member.name)
                        )
            if hasattr(tarfile, "data_filter"):
                tar.extractall(str(destination), filter="data")
            else:  # pragma: no cover - Python < 3.12
                tar.extractall(str(destination))

    def _apply_patches(self, source_dir: Path) -> None:
        patch_dir = self.config.patches_dir
        if not patch_dir.is_dir():
            LOGGER.debug("补丁目录 %s 不存在", patch_dir)
            return
        patches = sorted(
            list(patch_dir.glob("*.patch")) + list(patch_dir.glob("*.diff"))
        )
        if not patches:
            LOGGER.info("%s 中没有本地补丁", patch_dir)
            return
        marker = source_dir / ".kau-patches-applied"
        if marker.exists():
            LOGGER.info("当前编译目录已应用本地补丁")
            return
        if not self.runner.which("patch"):
            raise UpdateError("存在本地补丁，但系统没有 patch 命令")
        for patch in patches:
            LOGGER.info("正在应用补丁 %s", patch.name)
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
            LOGGER.info("使用 make defconfig；可能会缺少本机所需的驱动")
            self.runner.run(["make", "defconfig"], cwd=source_dir)
        else:
            config_file = source_dir / ".config"
            source = write_kernel_config(spec, self.current, config_file)
            LOGGER.info("使用内核配置 %s", source)
            self.runner.run(["make", "olddefconfig"], cwd=source_dir)

        scripts_config = source_dir / "scripts" / "config"
        if not scripts_config.exists():
            raise UpdateError("内核源码缺少 scripts/config")
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
                "无法通过 make kernelrelease 确定编译出的内核版本"
            )
        return kernel_release

    def _update_initramfs(self, kernel_release: str) -> None:
        if not self.config.update_initramfs:
            LOGGER.info("配置已禁用 initramfs 刷新")
            return
        if self.runner.which("update-initramfs"):
            mode = "-u" if initramfs_exists(kernel_release) else "-c"
            self.runner.run(["update-initramfs", mode, "-k", kernel_release])
            return
        if self.runner.which("dracut"):
            if initramfs_exists(kernel_release):
                LOGGER.info(
                    "%s 的 initramfs 已存在（由 installkernel 创建）",
                    kernel_release,
                )
                return
            image = Path("/boot/initramfs-{}.img".format(kernel_release))
            self.runner.run(["dracut", "--force", str(image), kernel_release])
            return
        if self.runner.which("mkinitcpio"):
            LOGGER.warning(
                "检测到 mkinitcpio，但 %s 没有对应的 preset；"
                "请先创建 preset（或使用正确配置运行 mkinitcpio）再重启",
                kernel_release,
            )
            return
        LOGGER.warning(
            "没有找到受支持的 initramfs 工具；重启前请检查 /boot"
        )

    def _update_bootloader(self) -> None:
        if not self.config.update_bootloader:
            LOGGER.info("配置已禁用引导项刷新")
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
                "检测到 systemd-boot；kernel-install 应已自动创建引导项"
            )
            return
        LOGGER.warning(
            "没有找到受支持的引导更新命令；重启前请手动把新内核加入引导项"
        )

    def _verify_install(self, kernel_release: str) -> None:
        modules = Path("/lib/modules") / kernel_release
        if not modules.is_dir():
            modules = Path("/usr/lib/modules") / kernel_release
        if not modules.is_dir():
            if boot_image_exists(kernel_release):
                LOGGER.warning(
                    "%s 没有模块目录；单体内核出现这种情况是正常的",
                    kernel_release,
                )
            else:
                raise UpdateError(
                    "模块没有安装到 /lib/modules/{}".format(
                        kernel_release
                    )
                )
        if not boot_image_exists(kernel_release):
            LOGGER.warning(
                "在 /boot 下找不到 %s 的启动镜像；"
                "请确认 installkernel/kernel-install 已创建引导项",
                kernel_release,
            )
        elif self.runner.which("bootctl"):
            entries = list(Path("/boot/loader/entries").glob("*{}*.conf".format(kernel_release)))
            if entries:
                LOGGER.info("systemd-boot 引导项: %s", entries[0].name)
        if modules.is_dir():
            LOGGER.info("模块已安装到 %s", modules)

    def _schedule_reboot(self, kernel_release: str) -> None:
        delay = self.config.reboot_delay_minutes
        if delay > 0:
            command = [
                "shutdown",
                "-r",
                "+{}".format(delay),
                "内核自动更新器：重启以启用 {}".format(kernel_release),
            ]
            LOGGER.info("将在 %d 分钟后重启", delay)
        else:
            command = ["shutdown", "-r", "now"]
            LOGGER.info("正在立即重启")
        self.runner.run(command)

    def _preflight(self, release: KernelRelease, options: UpdateOptions) -> None:
        if not is_linux():
            raise UpdateError("仅支持在 Linux 上更新内核")
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and geteuid() != 0:
            raise UpdateError("更新内核需要 root 权限，请使用 sudo 运行")

        if secure_boot_enabled(self.runner) is True and not options.force:
            raise UpdateError(
                "UEFI Secure Boot 已启用，而源码编译模式安装的是未签名内核。"
                "请自行签名或关闭 Secure Boot；确认风险后可加 --force 继续。"
            )

        if not release.checksums_url and not self.config.allow_unverified:
            raise UpdateError(
                "{} 没有发布 SHA-256 校验和。请使用 stable/longterm 版本，"
                "或在接受风险后设置 allow_unverified = true。".format(
                    release.version
                )
            )
        if self.config.verify_pgp and not self.config.pgp_keyring.is_file():
            raise UpdateError(
                "已启用 source.verify_pgp，但公钥环不存在: {}".format(
                    self.config.pgp_keyring
                )
            )
        if self.config.verify_pgp and not release.pgp:
            raise UpdateError(
                "已启用 source.verify_pgp，但 {} 没有 .tar.sign 签名".format(
                    release.version
                )
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
                "缺少编译依赖/命令: {}。请先安装发行版的内核编译工具链"
                "（gcc、make、bc、flex、bison、perl、libssl-dev/openssl-devel、"
                "dwarves/pahole、cpio）。".format(
                    ", ".join(missing)
                )
            )
        if self.config.update_initramfs and not any(
            self.runner.which(tool)
            for tool in ("update-initramfs", "dracut", "mkinitcpio")
        ):
            LOGGER.warning(
                "没有找到受支持的 initramfs 工具，新内核可能无法启动"
            )
        for optional in ("pahole", "cpio"):
            if not self.runner.which(optional):
                LOGGER.warning(
                    "缺少 %s，BTF 或 initramfs 生成可能失败",
                    optional,
                )

        spec = "defconfig" if options.defconfig else self.config.config_source
        find_kernel_config(spec, self.current)

        available = free_gb(self.config.work_dir)
        if available < self.config.min_free_gb:
            raise UpdateError(
                "{} 只剩 {:.1f} GiB，至少需要 {} GiB（updater.min_free_gb）"
                .format(
                    self.config.work_dir,
                    available,
                    self.config.min_free_gb,
                )
            )

        boot = Path("/boot")
        if boot.is_dir():
            boot_free = free_gb(boot)
            if boot_free < 0.5:
                LOGGER.warning(
                    "/boot 只剩 %.0f MiB，安装可能失败",
                    boot_free * 1024,
                )

        LOGGER.info(
            "预检通过：%s -> %s（来源: %s）",
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
        LOGGER.info("当前内核 : %s", self.current)
        LOGGER.info("目标内核 : %s (%s)", release.version, release.moniker)
        LOGGER.info("源码     : %s", release.source)
        LOGGER.info("编译任务 : %s", jobs)
        LOGGER.info(
            "安装后重启: %s",
            "是" if options.reboot else "否",
        )
        if not sys.stdin.isatty():
            raise UpdateError("需要确认；请重新运行并加 --yes")
        try:
            answer = input("编译并安装这个内核？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            raise UpdateError("已由用户取消")

    def _print_plan(self, release: KernelRelease, options: UpdateOptions) -> None:
        jobs = options.jobs or self.config.jobs or cpu_count()
        spec = "defconfig" if options.defconfig else self.config.config_source
        LOGGER.info("试运行计划")
        LOGGER.info("  当前内核 : %s", self.current)
        LOGGER.info("  目标内核 : %s (%s)", release.version, release.moniker)
        LOGGER.info("  源码     : %s", release.source)
        if release.checksums_url:
            LOGGER.info("  校验和   : %s", release.checksums_url)
        else:
            LOGGER.info("  校验和   : 不可用")
        LOGGER.info("  配置来源 : %s", spec)
        LOGGER.info("  本地版本 : %r", self.config.localversion)
        LOGGER.info("  编译目录 : %s/linux-*/", self.config.build_dir)
        LOGGER.info("  补丁目录 : %s", self.config.patches_dir)
        LOGGER.info("  编译命令 : make -j%d all", jobs)
        LOGGER.info("  安装命令 : make modules_install && make install")
        LOGGER.info(
            "  清理     : %s",
            "保留编译目录"
            if (options.keep_build or self.config.keep_build_dir)
            else "成功后删除编译目录",
        )
        LOGGER.info("  安装后重启: %s", "是" if options.reboot else "否")

    @staticmethod
    def _step(index: int, total: int, title: str) -> None:
        LOGGER.info("[%d/%d] %s", index, total, title)

    def _save_state(self) -> None:
        try:
            self.state_store.save(self.state)
        except OSError as exc:
            LOGGER.warning("无法保存更新状态: %s", exc)

