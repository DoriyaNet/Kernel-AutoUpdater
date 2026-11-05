import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kernel_autoupdater.cli import cmd_check, cmd_kernels, cmd_status, main
from kernel_autoupdater.config import Config
from kernel_autoupdater.kernelorg import _parse_feed

from tests.test_kernelorg import sample_payload


def sample_feed():
    return _parse_feed(json.dumps(sample_payload()))


def make_config(temp):
    config_path = Path(temp) / "config.ini"
    config_path.write_text(
        """[updater]
channel = stable
work_dir = {work}
log_file = {log}
auto_update = false

[build]
config_source = defconfig
""".format(work=Path(temp) / "work", log=Path(temp) / "kau.log"),
        encoding="utf-8",
    )
    return config_path


class CliTests(unittest.TestCase):
    def test_check_json_reports_update(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config.load(make_config(temp), explicit=True)
            output = io.StringIO()
            with patch("kernel_autoupdater.cli._fetch_feed", return_value=sample_feed()), \
                    patch("kernel_autoupdater.builder.running_kernel", return_value="6.8.0-45-generic"):
                with redirect_stdout(output):
                    code = cmd_check(SimpleNamespace(json=True, offline=False), config)
            self.assertEqual(code, 10)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["update_available"])
            self.assertEqual(payload["latest"], "7.3.1")

    def test_status_json(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config.load(make_config(temp), explicit=True)
            output = io.StringIO()
            with redirect_stdout(output):
                code = cmd_status(SimpleNamespace(json=True), config)
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["channel"], "stable")
            self.assertEqual(payload["installed"], [])

    def test_main_check_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = make_config(temp)
            output = io.StringIO()
            with patch("kernel_autoupdater.cli._fetch_feed", return_value=sample_feed()), \
                    patch("kernel_autoupdater.builder.running_kernel", return_value="6.8.0-45-generic"):
                with redirect_stdout(output):
                    code = main(["--config", str(config_path), "check", "--json"])
            self.assertEqual(code, 10)
            self.assertTrue(json.loads(output.getvalue())["update_available"])

    def test_kernels_json_handles_empty_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config.load(make_config(temp), explicit=True)
            output = io.StringIO()
            with patch("kernel_autoupdater.cli.installed_module_releases", return_value=[]):
                with redirect_stdout(output):
                    code = cmd_kernels(SimpleNamespace(json=True), config)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue()), [])


if __name__ == "__main__":
    unittest.main()

