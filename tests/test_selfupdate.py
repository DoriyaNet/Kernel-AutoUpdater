import io
import os
import shutil
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kernel_autoupdater import selfupdate


class VersionTests(unittest.TestCase):
    def test_version_key(self):
        self.assertGreater(
            selfupdate._version_key("0.10.0"),
            selfupdate._version_key("0.9.9"),
        )
        self.assertEqual(selfupdate._version_key("v1.2.3"), (1, 2, 3))

    def test_invalid_refs(self):
        for ref in ("", "..", "../x", "/x", "x/", "bad ref", "-x"):
            with self.subTest(ref=ref):
                with self.assertRaises(selfupdate.SelfUpdateError):
                    selfupdate._validate_ref(ref)

    def test_valid_refs(self):
        for ref in ("master", "v0.2.0", "feature/tui"):
            with self.subTest(ref=ref):
                self.assertEqual(selfupdate._validate_ref(ref), ref)


class CheckTests(unittest.TestCase):
    def test_check_only_reports_newer_version(self):
        output = io.StringIO()
        with patch.object(selfupdate, "__version__", "0.2.0"), patch.object(
            selfupdate, "fetch_remote_version", return_value="0.3.0"
        ):
            with redirect_stdout(output):
                code = selfupdate.run_self_update(check_only=True)
        self.assertEqual(code, 10)
        self.assertIn("0.3.0", output.getvalue())

    def test_check_only_reports_current_version(self):
        output = io.StringIO()
        with patch.object(selfupdate, "__version__", "0.2.0"), patch.object(
            selfupdate, "fetch_remote_version", return_value="0.2.0"
        ):
            with redirect_stdout(output):
                code = selfupdate.run_self_update(check_only=True)
        self.assertEqual(code, 0)
        self.assertIn("无需更新", output.getvalue())

    def test_find_source_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Kernel-AutoUpdater-master"
            (root / "scripts").mkdir(parents=True)
            (root / "src" / "kernel_autoupdater").mkdir(parents=True)
            (root / "scripts" / "install.sh").write_text("#!/bin/sh\n")
            (root / "src" / "kernel_autoupdater" / "__init__.py").write_text("")
            self.assertEqual(
                selfupdate._find_source_root(Path(temp)), root
            )

    def test_find_source_root_rejects_missing_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(selfupdate.SelfUpdateError):
                selfupdate._find_source_root(Path(temp))

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"),
        "requires POSIX and bash",
    )
    def test_update_runs_downloaded_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_root = root / "Kernel-AutoUpdater-test"
            (archive_root / "scripts").mkdir(parents=True)
            (archive_root / "src" / "kernel_autoupdater").mkdir(parents=True)
            (archive_root / "src" / "kernel_autoupdater" / "__init__.py").write_text("")
            marker = root / "marker.txt"
            (archive_root / "scripts" / "install.sh").write_text(
                "#!/bin/sh\n"
                "printf '%s:%s' \"$ENABLE_TIMER\" \"$PYTHON\" > \"$KAU_TEST_MARKER\"\n"
            )
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(archive_root, arcname=archive_root.name)

            def fake_download(ref, destination, timeout):
                shutil.copyfile(archive, destination)

            old_marker = os.environ.get("KAU_TEST_MARKER")
            os.environ["KAU_TEST_MARKER"] = str(marker)
            try:
                with patch.object(selfupdate, "__version__", "0.2.0"), \
                        patch.object(
                            selfupdate, "fetch_remote_version", return_value="0.3.0"
                        ), \
                        patch.object(
                            selfupdate, "_download_archive", side_effect=fake_download
                        ), \
                        patch.object(
                            selfupdate, "_timer_enabled", return_value=False
                        ), \
                        patch.object(
                            selfupdate.os, "geteuid", return_value=0, create=True
                        ), \
                        redirect_stdout(io.StringIO()):
                    code = selfupdate.run_self_update(assume_yes=True)
            finally:
                if old_marker is None:
                    os.environ.pop("KAU_TEST_MARKER", None)
                else:
                    os.environ["KAU_TEST_MARKER"] = old_marker

            self.assertEqual(code, 0)
            self.assertTrue(marker.read_text().startswith("0:"))


if __name__ == "__main__":
    unittest.main()

