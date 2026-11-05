import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kernel_autoupdater.builder import CheckResult
from kernel_autoupdater.cli import (
    cmd_auto,
    cmd_check,
    cmd_kernels,
    cmd_menu,
    cmd_status,
    main,
    _menu_self_update,
)
from kernel_autoupdater.config import Config
from kernel_autoupdater.kernelorg import KernelRelease, _parse_feed
from kernel_autoupdater.packages import PackageUpdate

from tests.test_kernelorg import sample_payload


def sample_feed():
    return _parse_feed(json.dumps(sample_payload()))


def make_config(temp, method="build"):
    config_path = Path(temp) / "config.ini"
    config_path.write_text(
        """[updater]
update_method = {method}
channel = stable
work_dir = {work}
log_file = {log}
auto_update = false

[build]
config_source = defconfig
""".format(
            method=method,
            work=Path(temp) / "work",
            log=Path(temp) / "kau.log",
        ),
        encoding="utf-8",
    )
    return config_path


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


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

    def test_package_check_json(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config.load(make_config(temp), explicit=True)
            package = PackageUpdate(
                manager="apt",
                packages=("linux-image-generic",),
                installed_version="6.8.0-79.79",
                candidate_version="6.8.0-80.80",
                kernel_version="6.8.0",
                update_available=True,
            )
            check = CheckResult(
                current="6.8.0-79-generic",
                current_base="6.8.0",
                channel="package:apt",
                latest=KernelRelease(
                    version="6.8.0-80.80",
                    moniker="apt",
                    source=package.description,
                ),
                update_available=True,
                method="package",
                package=package,
            )
            output = io.StringIO()
            with patch(
                "kernel_autoupdater.cli.check_package_update",
                return_value=check,
            ):
                with redirect_stdout(output):
                    code = cmd_check(
                        SimpleNamespace(
                            json=True, offline=False, method="package"
                        ),
                        config,
                    )
            self.assertEqual(code, 10)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["method"], "package")
            self.assertEqual(payload["package_manager"], "apt")
            self.assertEqual(payload["package_candidate"], "6.8.0-80.80")

    def test_package_auto_does_not_fetch_kernelorg(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = make_config(temp, method="package")
            config = Config.load(config_path, explicit=True)
            package = PackageUpdate(
                manager="apt",
                packages=("linux-image-generic",),
                installed_version="6.8.0-79.79",
                candidate_version="6.8.0-80.80",
                kernel_version="6.8.0",
                update_available=True,
            )
            check = CheckResult(
                current="6.8.0-79-generic",
                current_base="6.8.0",
                channel="package:apt",
                latest=KernelRelease(
                    version="6.8.0-80.80",
                    moniker="apt",
                    source=package.description,
                ),
                update_available=True,
                method="package",
                package=package,
            )
            with patch(
                "kernel_autoupdater.cli.check_package_update",
                return_value=check,
            ), patch("kernel_autoupdater.cli._fetch_feed") as fetch:
                code = cmd_auto(
                    SimpleNamespace(dry_run=False, method=None), config
                )
            self.assertEqual(code, 0)
            fetch.assert_not_called()

    def test_package_update_dry_run_skips_install(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = make_config(temp, method="package")
            config = Config.load(config_path, explicit=True)
            package = PackageUpdate(
                manager="apt",
                packages=("linux-image-generic",),
                installed_version="6.8.0-79.79",
                candidate_version="6.8.0-80.80",
                kernel_version="6.8.0",
                update_available=True,
            )

            class Backend:
                name = "apt"
                packages = ("linux-image-generic",)

                def inspect(self, current):
                    return package

                def install_command(self):
                    return ["apt-get", "install", "linux-image-generic"]

                def install(self):
                    raise AssertionError("dry-run must not install")

            with patch(
                "kernel_autoupdater.builder.distro_packages.build_backend",
                return_value=Backend(),
            ), patch(
                "kernel_autoupdater.builder.running_kernel",
                return_value="6.8.0-79-generic",
            ):
                code = main(
                    [
                        "--config",
                        str(config_path),
                        "update",
                        "--dry-run",
                        "--method",
                        "package",
                    ]
                )
            self.assertEqual(code, 0)

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

    def test_no_command_non_tty_prints_help(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = make_config(temp)
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO()), patch("sys.stdout", output):
                code = main(["--config", str(config_path)])
            self.assertEqual(code, 0)
            self.assertIn("usage: kau", output.getvalue())

    def test_no_command_on_tty_opens_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = make_config(temp)
            with patch("sys.stdin", _FakeTTY()), patch("sys.stdout", _FakeTTY()), \
                    patch("kernel_autoupdater.menu.run_menu", return_value=0) as run:
                code = main(["--config", str(config_path)])
            self.assertEqual(code, 0)
            run.assert_called_once()

    def test_menu_subcommand_uses_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = make_config(temp)
            with patch("kernel_autoupdater.menu.run_menu", return_value=7):
                code = main(["--config", str(config_path), "menu"])
            self.assertEqual(code, 7)

    def test_self_update_subcommand(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = make_config(temp)
            with patch(
                "kernel_autoupdater.cli.run_self_update", return_value=10
            ) as run:
                code = main(
                    ["--config", str(config_path), "self-update", "--check"]
                )
            self.assertEqual(code, 10)
            run.assert_called_once_with(
                ref="master", check_only=True, force=False, assume_yes=False
            )

    def test_menu_wires_self_update_action(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config.load(make_config(temp), explicit=True)
            with patch(
                "kernel_autoupdater.menu.run_menu", return_value=0
            ) as run:
                code = cmd_menu(SimpleNamespace(), config)
            self.assertEqual(code, 0)
            actions = run.call_args[0][2]
            self.assertIn("self_update", actions)
            self.assertIn("update", actions)
            self.assertIn("update_reboot", actions)

    def test_menu_self_update_skips_cli_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config.load(make_config(temp), explicit=True)
            with patch(
                "kernel_autoupdater.cli.run_self_update", return_value=0
            ) as run:
                code = _menu_self_update(config)
            self.assertEqual(code, 0)
            run.assert_called_once_with(
                ref="master",
                check_only=False,
                force=False,
                assume_yes=True,
            )


if __name__ == "__main__":
    unittest.main()

