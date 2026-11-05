import tempfile
import unittest
from pathlib import Path

from kernel_autoupdater.config import Config
from kernel_autoupdater.config_editor import (
    DEFAULT_CONFIG_TEXT,
    get_value,
    read_config_text,
    set_value,
    write_config_atomic,
)


class ConfigEditorTests(unittest.TestCase):
    def test_default_config_is_valid_and_has_all_editable_options(self):
        expected = [
            ("updater", "update_method"),
            ("updater", "channel"),
            ("updater", "auto_update"),
            ("updater", "auto_reboot"),
            ("updater", "reboot_delay_minutes"),
            ("updater", "min_free_gb"),
            ("updater", "feed_url"),
            ("updater", "feed_timeout"),
            ("updater", "download_timeout"),
            ("updater", "work_dir"),
            ("updater", "log_file"),
            ("source", "verify_pgp"),
            ("source", "allow_unverified"),
            ("source", "pgp_keyring"),
            ("source", "patches_dir"),
            ("build", "config_source"),
            ("build", "localversion"),
            ("build", "jobs"),
            ("build", "keep_build_dir"),
            ("build", "clear_distro_keys"),
            ("packages", "manager"),
            ("packages", "kernel_packages"),
            ("packages", "refresh_package_index"),
            ("packages", "min_free_gb"),
            ("install", "update_initramfs"),
            ("install", "update_bootloader"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
            config = Config.load(path, explicit=True)
        self.assertEqual(config.channel, "stable")
        self.assertEqual(config.update_method, "build")
        for section, key in expected:
            with self.subTest(section=section, key=key):
                self.assertIsNotNone(get_value(DEFAULT_CONFIG_TEXT, section, key))

    def test_read_missing_file_returns_default_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.ini"
            self.assertEqual(read_config_text(path), DEFAULT_CONFIG_TEXT)
            self.assertFalse(path.exists())

    def test_read_existing_file_returns_exact_text(self):
        text = "[updater]\r\nchannel = mainline\r\n"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            path.write_bytes(text.encode("utf-8"))
            self.assertEqual(read_config_text(path), text)

    def test_replace_preserves_comments_indentation_and_unrelated_lines(self):
        text = (
            "; top comment\n"
            "[updater]\n"
            "# keep this comment\n"
            "  channel = stable\n"
            "auto_update = false\n"
            "\n"
            "[source]\n"
            "verify_pgp = false\n"
        )
        edited = set_value(text, "updater", "channel", "mainline")
        self.assertEqual(
            edited,
            "; top comment\n"
            "[updater]\n"
            "# keep this comment\n"
            "  channel = mainline\n"
            "auto_update = false\n"
            "\n"
            "[source]\n"
            "verify_pgp = false\n",
        )

    def test_replace_section_and_key_case_insensitively(self):
        text = "[Updater]\nChannel = stable\n"
        edited = set_value(text, "updater", "channel", "longterm")
        self.assertEqual(edited, "[Updater]\nChannel = longterm\n")

    def test_insert_missing_key_before_next_section(self):
        text = (
            "[updater]\n"
            "channel = stable\n"
            "# trailing section comment\n"
            "\n"
            "[source]\n"
            "verify_pgp = false\n"
        )
        edited = set_value(text, "updater", "auto_update", "true")
        self.assertEqual(
            edited,
            "[updater]\n"
            "channel = stable\n"
            "# trailing section comment\n"
            "auto_update = true\n"
            "\n"
            "[source]\n"
            "verify_pgp = false\n",
        )

    def test_insert_missing_key_preserves_section_indentation(self):
        text = "[build]\n  config_source = auto\n"
        edited = set_value(text, "build", "jobs", "8")
        self.assertEqual(
            edited,
            "[build]\n  config_source = auto\n  jobs = 8\n",
        )

    def test_insert_missing_key_at_eof_without_final_newline(self):
        text = "[install]\nupdate_initramfs = true"
        edited = set_value(text, "install", "update_bootloader", "false")
        self.assertEqual(
            edited,
            "[install]\nupdate_initramfs = true\nupdate_bootloader = false",
        )

    def test_append_missing_section(self):
        text = "[updater]\nchannel = stable\n"
        edited = set_value(text, "install", "update_initramfs", "true")
        self.assertEqual(
            edited,
            "[updater]\nchannel = stable\n\n"
            "[install]\nupdate_initramfs = true\n",
        )

    def test_append_missing_section_does_not_add_extra_blank_line(self):
        text = "[updater]\nchannel = stable\n\n"
        edited = set_value(text, "install", "update_initramfs", "true")
        self.assertEqual(
            edited,
            "[updater]\nchannel = stable\n\n"
            "[install]\nupdate_initramfs = true\n",
        )

    def test_append_section_to_text_without_sections(self):
        text = "# just a comment\n"
        edited = set_value(text, "install", "update_initramfs", "true")
        self.assertEqual(
            edited,
            "# just a comment\n\n[install]\nupdate_initramfs = true\n",
        )

    def test_append_section_to_empty_text(self):
        edited = set_value("", "install", "update_initramfs", "true")
        self.assertEqual(
            edited, "[install]\nupdate_initramfs = true"
        )
        self.assertEqual(
            get_value(edited, "install", "update_initramfs"), "true"
        )

    def test_crlf_is_normalized_to_lf(self):
        text = "[updater]\r\nchannel = stable\r\n# keep\r\n"
        edited = set_value(text, "updater", "channel", "mainline")
        self.assertNotIn("\r", edited)
        self.assertEqual(
            edited, "[updater]\nchannel = mainline\n# keep\n"
        )

    def test_get_value_is_case_insensitive_and_reports_missing(self):
        text = "[Updater]\nChannel = stable\n"
        self.assertEqual(get_value(text, "updater", "channel"), "stable")
        self.assertEqual(get_value(text, "UPDATER", "CHANNEL"), "stable")
        self.assertIsNone(get_value(text, "updater", "missing"))
        self.assertIsNone(get_value(text, "missing", "channel"))
        self.assertIsNone(get_value(text, "updater", "chan"))

    def test_get_value_handles_crlf(self):
        text = "[updater]\r\nchannel = stable\r\n"
        self.assertEqual(get_value(text, "updater", "channel"), "stable")

    def test_invalid_identifiers_are_rejected(self):
        for section in ("", "bad section", "a]b", "[evil]", "a\nb"):
            with self.subTest(section=section):
                with self.assertRaises(ValueError):
                    set_value("[updater]\n", section, "channel", "stable")
        for key in ("", "bad key", "a=b", "a\nb", "a]b"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    set_value("[updater]\n", "updater", key, "value")

    def test_multiline_values_are_rejected(self):
        for value in ("one\ntwo", "one\rtwo"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    set_value("[updater]\n", "updater", "channel", value)

    def test_atomic_write_creates_parent_and_replaces_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "config.ini"
            write_config_atomic(path, "first\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "first\n")
            write_config_atomic(path, "second\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "second\n")
            leftovers = [
                item.name
                for item in path.parent.iterdir()
                if item.name != "config.ini"
            ]
            self.assertEqual(leftovers, [])

    def test_atomic_write_preserves_text_verbatim(self):
        text = "[updater]\nchannel = stable\n"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            write_config_atomic(path, text)
            self.assertEqual(path.read_text(encoding="utf-8"), text)


if __name__ == "__main__":
    unittest.main()
