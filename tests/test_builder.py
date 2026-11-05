import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kernel_autoupdater.builder import (
    UpdateError,
    UpdateOptions,
    Updater,
    check_for_update,
    check_package_update,
)
from kernel_autoupdater.config import Config
from kernel_autoupdater.kernelorg import _parse_feed, release_for_version
from kernel_autoupdater.packages import PackageUpdate
from kernel_autoupdater.runner import CommandRunner
from kernel_autoupdater.state import State, StateStore

from tests.test_kernelorg import sample_payload


def sample_feed():
    return _parse_feed(json.dumps(sample_payload()))


class CheckTests(unittest.TestCase):
    def test_update_available(self):
        result = check_for_update(
            Config(channel="stable"),
            State(),
            feed=sample_feed(),
            current="6.8.0-45-generic",
        )
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest.version, "7.3.1")
        self.assertEqual(result.current_base, "6.8.0")

    def test_already_current(self):
        result = check_for_update(
            Config(channel="stable"),
            State(),
            feed=sample_feed(),
            current="7.3.1-kau",
        )
        self.assertFalse(result.update_available)

    def test_installed_release_is_reported(self):
        state = State()
        state.record_install("7.3.0", "7.3.0-kau", "x", "6.8.0")
        result = check_for_update(
            Config(channel="stable"), state, feed=sample_feed(), current="6.8.0"
        )
        self.assertEqual(result.installed_release, "7.3.0-kau")

    def test_installed_latest_kernel_waits_for_reboot(self):
        state = State()
        state.record_install("7.3.1", "7.3.1-kau", "x", "6.8.0")
        with patch("kernel_autoupdater.builder.boot_image_exists", return_value=True):
            result = check_for_update(
                Config(channel="stable"), state, feed=sample_feed(), current="6.8.0"
            )
        self.assertTrue(result.pending_reboot)
        self.assertFalse(result.update_available)

    def test_package_update_is_reported(self):
        package = PackageUpdate(
            manager="apt",
            packages=("linux-image-generic",),
            installed_version="6.8.0-79.79",
            candidate_version="6.8.0-80.80",
            kernel_version="6.8.0",
            update_available=True,
        )

        class Backend:
            def inspect(self, current):
                return package

        result = check_package_update(
            Config(update_method="package"),
            State(),
            runner=CommandRunner(dry_run=True),
            current="6.8.0-79-generic",
            backend=Backend(),
        )
        self.assertEqual(result.method, "package")
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest.version, "6.8.0")
        self.assertEqual(result.latest_version, "6.8.0-80.80")
        self.assertEqual(str(result.latest.parsed_version), "6.8.0")
        self.assertEqual(result.package.candidate_version, "6.8.0-80.80")

    def test_package_pending_reboot_is_not_an_update(self):
        package = PackageUpdate(
            manager="apt",
            packages=("linux-image-generic",),
            installed_version="6.8.0-80.80",
            candidate_version="6.8.0-80.80",
            kernel_version="6.8.0",
            update_available=False,
            pending_reboot=True,
        )

        class Backend:
            def inspect(self, current):
                return package

        result = check_package_update(
            Config(update_method="package"),
            State(),
            runner=CommandRunner(dry_run=True),
            current="6.8.0-79-generic",
            backend=Backend(),
            installed_releases=lambda: ["6.8.0-80-generic"],
        )
        self.assertTrue(result.pending_reboot)
        self.assertFalse(result.update_available)
        self.assertEqual(result.installed_release, "6.8.0-80-generic")


class ResolveTests(unittest.TestCase):
    def test_target_not_in_feed_uses_cdn_url(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(work_dir=Path(temp))
            updater = Updater(
                config,
                CommandRunner(dry_run=True),
                StateStore(config.state_path),
                current="6.8.0",
            )
            release = updater._resolve_release(
                sample_feed(), UpdateOptions(target="7.2.9")
            )
            self.assertEqual(
                release.source,
                "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.9.tar.xz",
            )

    def test_zero_patch_target_uses_short_archive_name(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(work_dir=Path(temp))
            updater = Updater(
                config,
                CommandRunner(dry_run=True),
                StateStore(config.state_path),
                current="6.7.0",
            )
            release = updater._resolve_release(
                sample_feed(), UpdateOptions(target="6.8.0")
            )
            self.assertEqual(
                release.source,
                "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.tar.xz",
            )


class PackageExecuteTests(unittest.TestCase):
    def _updater(self, temp):
        config = Config(update_method="package", work_dir=Path(temp))
        return config, Updater(
            config,
            CommandRunner(dry_run=True),
            StateStore(config.state_path),
            current="6.8.0-79-generic",
        )

    def test_dry_run_does_not_install(self):
        with tempfile.TemporaryDirectory() as temp:
            config, updater = self._updater(temp)
            package = PackageUpdate(
                manager="apt",
                packages=("linux-image-generic",),
                installed_version="6.8.0-79.79",
                candidate_version="6.8.0-80.80",
                kernel_version="6.8.0",
                update_available=True,
            )

            class Backend:
                packages = ("linux-image-generic",)
                name = "apt"

                def inspect(self, current):
                    return package

                def install_command(self):
                    return ["apt-get", "install", "linux-image-generic"]

                def install(self):
                    raise AssertionError("dry-run must not install")

            with patch(
                "kernel_autoupdater.builder.distro_packages.build_backend",
                return_value=Backend(),
            ):
                result = updater.execute(UpdateOptions(dry_run=True))
            self.assertEqual(result.status, "dry-run")

    def test_build_only_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            _, updater = self._updater(temp)
            with self.assertRaises(UpdateError):
                updater.execute(UpdateOptions(dry_run=True, jobs=4))

    def test_release_detection_prefers_candidate_abi(self):
        package = PackageUpdate(
            manager="apt",
            packages=("linux-image-generic",),
            installed_version="6.8.0-78.78",
            candidate_version="6.8.0-79.80",
            kernel_version="6.8.0",
            update_available=True,
        )
        release = Updater._detect_package_release(
            package,
            {"6.8.0-79-generic", "6.8.0-80-generic"},
            {"6.8.0-79-generic", "6.8.0-80-generic"},
        )
        self.assertEqual(release, "6.8.0-79-generic")

    def test_release_detection_handles_new_dnf_kernel(self):
        package = PackageUpdate(
            manager="dnf",
            packages=("kernel",),
            installed_version="6.11.3-300.fc41",
            candidate_version="6.11.4-301.fc41",
            kernel_version="6.11.4",
            update_available=True,
        )
        release = Updater._detect_package_release(
            package,
            {"6.11.3-300.fc41.x86_64"},
            {
                "6.11.3-300.fc41.x86_64",
                "6.11.4-301.fc41.x86_64",
            },
        )
        self.assertEqual(release, "6.11.4-301.fc41.x86_64")

    def test_release_detection_handles_ubuntu_dotted_version(self):
        package = PackageUpdate(
            manager="apt",
            packages=("linux-image-generic",),
            installed_version="5.15.0.163.160",
            candidate_version="5.15.0.164.159",
            kernel_version="5.15.0",
            update_available=True,
        )
        release = Updater._detect_package_release(
            package,
            {"5.15.0-163-generic", "5.15.0-164-generic"},
            {"5.15.0-163-generic", "5.15.0-164-generic"},
        )
        self.assertEqual(release, "5.15.0-164-generic")

    def test_installs_package_and_records_release(self):
        with tempfile.TemporaryDirectory() as temp:
            package = PackageUpdate(
                manager="apt",
                packages=("linux-image-generic",),
                installed_version="6.8.0-79.79",
                candidate_version="6.8.0-80.80",
                kernel_version="6.8.0",
                update_available=True,
            )
            installed = []

            class Backend:
                name = "apt"
                packages = ("linux-image-generic",)

                def describe(self):
                    return "apt (linux-image-generic)"

                def inspect(self, current):
                    return package

                def install_command(self):
                    return ["apt-get", "install", "linux-image-generic"]

                def install(self):
                    installed.append(True)

            config = Config(
                update_method="package",
                refresh_package_index=False,
                work_dir=Path(temp),
            )
            updater = Updater(
                config,
                CommandRunner(dry_run=True),
                StateStore(config.state_path),
                current="6.8.0-79-generic",
            )

            with patch(
                "kernel_autoupdater.builder.distro_packages.build_backend",
                return_value=Backend(),
            ), patch.object(Updater, "_preflight_package"), patch.object(
                Updater, "_verify_install"
            ), patch(
                "kernel_autoupdater.builder.installed_module_releases",
                side_effect=[set(), {"6.8.0-80-generic"}],
            ):
                result = updater.execute(UpdateOptions(assume_yes=True))

            self.assertEqual(result.status, "installed")
            self.assertTrue(installed)
            saved = StateStore(config.state_path).load()
            self.assertEqual(saved.installed[0].release, "6.8.0-80-generic")
            self.assertEqual(saved.installed[0].version, "6.8.0-80.80")


class ExtractTests(unittest.TestCase):
    def _tar_with(self, path, members):
        with tarfile.open(str(path), "w:gz") as tar:
            for name, payload in members:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

    def test_safe_extract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "src.tar.xz"
            # tarfile writes gzip despite the xz extension, but "r:*" detects it.
            self._tar_with(archive, [("linux-1.0/Makefile", b"all:\n")])
            destination = root / "build"
            Updater._safe_extract(archive, destination)
            self.assertTrue((destination / "linux-1.0" / "Makefile").exists())

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "evil.tar.gz"
            self._tar_with(archive, [("../escape", b"x")])
            with self.assertRaises(UpdateError):
                Updater._safe_extract(archive, root / "build")
            self.assertFalse((root / "escape").exists())

    def test_rejects_absolute_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "evil-link.tar.gz"
            with tarfile.open(str(archive), "w:gz") as tar:
                info = tarfile.TarInfo("linux-1.0/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            with self.assertRaises(UpdateError):
                Updater._safe_extract(archive, root / "build")

    def test_extract_uses_archive_top_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "linux-6.8.tar.xz"
            self._tar_with(archive, [("linux-6.8/Makefile", b"all:\n")])
            config = Config(work_dir=root / "work")
            updater = Updater(
                config,
                CommandRunner(dry_run=True),
                StateStore(config.state_path),
                current="6.7.0",
            )
            source = updater._extract_source(
                archive, release_for_version("6.8.0"), UpdateOptions()
            )
            self.assertEqual(source.name, "linux-6.8")


if __name__ == "__main__":
    unittest.main()

