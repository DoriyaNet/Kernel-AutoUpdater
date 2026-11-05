import tempfile
import unittest
from pathlib import Path

from kernel_autoupdater.config import Config, ConfigError


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = Config()
        self.assertEqual(config.channel, "stable")
        self.assertEqual(config.update_method, "build")
        self.assertEqual(config.package_manager, "auto")
        self.assertEqual(config.kernel_packages, "auto")
        self.assertFalse(config.auto_update)
        self.assertFalse(config.auto_reboot)

    def test_load_full_config(self):
        text = """[updater]
channel = longterm:6.12
work_dir = /tmp/kau-work
log_file = /tmp/kau.log
auto_update = yes
auto_reboot = true
reboot_delay_minutes = 15
min_free_gb = 20
feed_timeout = 10
download_timeout = 300

[source]
verify_pgp = true
pgp_keyring = /tmp/keyring.gpg
allow_unverified = false
patches_dir = /tmp/patches

[build]
config_source = /tmp/config
localversion = -test
jobs = 8

[packages]
manager = apt
kernel_packages = linux-image-generic,linux-headers-generic
refresh_package_index = false
min_free_gb = 3

[install]
update_initramfs = false
update_bootloader = yes
"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            path.write_text(text, encoding="utf-8")
            config = Config.load(path, explicit=True)
        self.assertEqual(config.channel, "longterm:6.12")
        self.assertTrue(config.auto_update)
        self.assertTrue(config.auto_reboot)
        self.assertEqual(config.reboot_delay_minutes, 15)
        self.assertEqual(config.min_free_gb, 20)
        self.assertTrue(config.verify_pgp)
        self.assertEqual(config.localversion, "-test")
        self.assertEqual(config.jobs, 8)
        self.assertEqual(config.package_manager, "apt")
        self.assertEqual(
            config.kernel_packages,
            "linux-image-generic,linux-headers-generic",
        )
        self.assertFalse(config.refresh_package_index)
        self.assertEqual(config.package_min_free_gb, 3)
        self.assertFalse(config.update_initramfs)
        self.assertTrue(config.update_bootloader)
        self.assertEqual(config.work_dir, Path("/tmp/kau-work"))

    def test_invalid_values(self):
        cases = [
            "[updater]\nauto_update = maybe\n",
            "[updater]\nchannel = beta\n",
            "[updater]\nfeed_url =\n",
            "[updater]\nmin_free_gb = -1\n",
            "[updater]\nupdate_method = compile\n",
            "[packages]\nmanager = yum\n",
            "[packages]\nkernel_packages =\n",
            "[build]\njobs = many\n",
        ]
        for text in cases:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.ini"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    Config.load(path, explicit=True)

    def test_missing_explicit_config(self):
        with self.assertRaises(ConfigError):
            Config.load(Path("/path/that/does/not/exist"), explicit=True)
        config = Config.load(Path("/path/that/does/not/exist"), explicit=False)
        self.assertTrue(config.auto_update is False)

    def test_log_file_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.ini"
            path.write_text("[updater]\nlog_file = none\n", encoding="utf-8")
            config = Config.load(path, explicit=True)
            self.assertIsNone(config.log_file)


if __name__ == "__main__":
    unittest.main()

