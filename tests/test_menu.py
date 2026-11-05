import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kernel_autoupdater import menu
from kernel_autoupdater.config import Config
from kernel_autoupdater.menu import _CHANNEL_SETTING, _save_setting


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def _noop_actions():
    return {key: (lambda config: 0) for key in menu.REQUIRED_ACTIONS}


class MenuFallbackTests(unittest.TestCase):
    def test_action_stream_splits_lines_and_flushes(self):
        lines = []
        stream = menu._ActionStream(lines.append)
        stream.write("first\nsecond")
        stream.flush()
        self.assertEqual(lines, ["first", "second"])

    def test_clean_output_removes_ansi(self):
        self.assertEqual(menu._clean_output("\x1b[32mgreen\x1b[0m\r"), "green")

    def test_wrap_output_lines_preserves_long_output(self):
        wrapped = menu._wrap_output_lines(
            ["install: " + "x" * 30], width=20
        )
        self.assertEqual("".join(wrapped), "install: " + "x" * 30)
        self.assertTrue(
            all(menu._display_width(line) <= 19 for line in wrapped)
        )

    def test_wrap_output_lines_counts_wide_characters(self):
        wrapped = menu._wrap_output_lines(["内核更新" * 4], width=10)
        self.assertEqual("".join(wrapped), "内核更新" * 4)
        self.assertTrue(
            all(menu._display_width(line) <= 9 for line in wrapped)
        )

    def test_main_menu_shows_current_update_method(self):
        labels = menu._main_labels(Config(update_method="package"))
        self.assertIn("更新方式：package（切换）", labels)
        self.assertIn("method", [key for _, key in menu._MAIN_ENTRIES])

    def test_non_tty_returns_2_without_importing_curses(self):
        self.assertNotIn("curses", menu.__dict__)
        stderr = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO()), mock.patch(
            "sys.stdout", io.StringIO()
        ), mock.patch("sys.stderr", stderr):
            code = menu.run_menu(
                Config(), Path("config.ini"), _noop_actions(), "0.3.0"
            )
        self.assertEqual(code, 2)
        self.assertIn("真实终端", stderr.getvalue())
        self.assertNotIn("curses", menu.__dict__)

    def test_non_tty_stdin_returns_2(self):
        with mock.patch("sys.stdin", io.StringIO()), mock.patch(
            "sys.stdout", _FakeTTY()
        ), mock.patch("sys.stderr", io.StringIO()):
            code = menu.run_menu(
                Config(), Path("config.ini"), _noop_actions(), "0.3.0"
            )
        self.assertEqual(code, 2)

    def test_non_tty_stdout_returns_2(self):
        with mock.patch("sys.stdin", _FakeTTY()), mock.patch(
            "sys.stdout", io.StringIO()
        ), mock.patch("sys.stderr", io.StringIO()):
            code = menu.run_menu(
                Config(), Path("config.ini"), _noop_actions(), "0.3.0"
            )
        self.assertEqual(code, 2)

    def test_fallback_happens_before_action_validation(self):
        with mock.patch("sys.stdin", io.StringIO()), mock.patch(
            "sys.stdout", io.StringIO()
        ), mock.patch("sys.stderr", io.StringIO()):
            code = menu.run_menu(Config(), Path("config.ini"), {}, "0.3.0")
        self.assertEqual(code, 2)

    def test_import_failure_returns_2_on_a_tty(self):
        import builtins

        real_import = builtins.__import__
        stderr = io.StringIO()

        def failing_import(name, *args, **kwargs):
            if name == "curses":
                raise ImportError("curses is not available")
            return real_import(name, *args, **kwargs)

        with mock.patch("sys.stdin", _FakeTTY()), mock.patch(
            "sys.stdout", _FakeTTY()
        ), mock.patch("sys.stderr", stderr), mock.patch(
            "builtins.__import__", side_effect=failing_import
        ):
            code = menu.run_menu(
                Config(), Path("config.ini"), _noop_actions(), "0.3.0"
            )

        self.assertEqual(code, 2)
        self.assertIn("不可用", stderr.getvalue())


class MenuSaveTests(unittest.TestCase):
    def test_invalid_value_restores_previous_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            previous = "[updater]\nchannel = stable\n# keep this\n"
            path.write_text(previous, encoding="utf-8")
            config = Config.load(path, explicit=True)

            new_config, message = _save_setting(
                config, path, _CHANNEL_SETTING, "beta"
            )

            self.assertIsNone(new_config)
            self.assertIn("恢复", message)
            self.assertEqual(path.read_text(encoding="utf-8"), previous)

    def test_invalid_value_on_missing_file_removes_created_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            new_config, message = _save_setting(
                Config(), path, _CHANNEL_SETTING, "beta"
            )
            self.assertIsNone(new_config)
            self.assertIn("恢复", message)
            self.assertFalse(path.exists())

    def test_valid_value_is_saved_and_config_is_reloaded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            path.write_text(
                "[build]\nlocalversion = -old\n", encoding="utf-8"
            )
            config = Config.load(path, explicit=True)

            new_config, message = _save_setting(
                config,
                path,
                menu._Setting(
                    "Local version",
                    "build",
                    "localversion",
                    "text",
                    "localversion",
                ),
                "-new",
            )

            self.assertIsNotNone(new_config)
            self.assertEqual(new_config.localversion, "-new")
            self.assertIn("已保存", message)
            self.assertIn(
                "localversion = -new", path.read_text(encoding="utf-8")
            )

    def test_os_error_reports_clear_message_without_restoring(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            previous = "[updater]\nchannel = stable\n"
            path.write_text(previous, encoding="utf-8")
            config = Config.load(path, explicit=True)

            with mock.patch(
                "kernel_autoupdater.menu.write_config_atomic",
                side_effect=OSError("permission denied"),
            ):
                new_config, message = _save_setting(
                    config, path, _CHANNEL_SETTING, "mainline"
                )

            self.assertIsNone(new_config)
            self.assertIn("无法保存", message)
            self.assertIn("permission denied", message)
            self.assertEqual(path.read_text(encoding="utf-8"), previous)


if __name__ == "__main__":
    unittest.main()
