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
)
from kernel_autoupdater.config import Config
from kernel_autoupdater.kernelorg import _parse_feed, release_for_version
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

