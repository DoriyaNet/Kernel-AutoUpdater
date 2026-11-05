import unittest

from kernel_autoupdater.versioning import (
    Channel,
    KernelVersion,
    VersionError,
    base_version,
    is_newer,
)


class KernelVersionTests(unittest.TestCase):
    def test_parse_plain_and_rc(self):
        self.assertEqual(KernelVersion.parse("6.12.5"), KernelVersion(6, 12, 5))
        self.assertEqual(KernelVersion.parse("v7.3.1"), KernelVersion(7, 3, 1))
        self.assertEqual(
            KernelVersion.parse("7.4-rc1"), KernelVersion(7, 4, 0, 1)
        )
        self.assertEqual(
            KernelVersion.parse("6.12.5-rc2"), KernelVersion(6, 12, 5, 2)
        )

    def test_distro_and_local_suffix_is_ignored(self):
        parsed = KernelVersion.parse("6.8.0-45-generic")
        self.assertEqual(parsed, KernelVersion(6, 8, 0))
        self.assertEqual(base_version("6.8.0-45-generic"), "6.8.0")
        self.assertEqual(base_version("7.3.1-kau"), "7.3.1")

    def test_release_sorts_after_rc(self):
        self.assertLess(
            KernelVersion.parse("7.3.1-rc1"), KernelVersion.parse("7.3.1")
        )
        self.assertLess(
            KernelVersion.parse("7.3.1"), KernelVersion.parse("7.4-rc1")
        )
        self.assertGreater(
            KernelVersion.parse("7.3.10"), KernelVersion.parse("7.3.9")
        )
        self.assertEqual(KernelVersion.parse("7.4-rc1").tag, "7.4-rc1")
        self.assertEqual(KernelVersion.parse("6.8.0").tag, "6.8")
        self.assertEqual(KernelVersion.parse("6.12.5").tag, "6.12.5")

    def test_is_newer(self):
        self.assertTrue(is_newer("7.3.1", "7.2.15"))
        self.assertFalse(is_newer("6.18.60", "7.3.1"))

    def test_invalid_versions(self):
        for value in ("", "linux", "7", "7.x.1", "7.3.1.2"):
            with self.assertRaises(VersionError):
                KernelVersion.parse(value)

    def test_channel_parsing(self):
        self.assertEqual(Channel.parse("stable"), Channel("stable"))
        self.assertEqual(
            Channel.parse("longterm:6.12"), Channel("longterm", (6, 12))
        )
        self.assertEqual(
            Channel.parse("LONGTERM/6.6"), Channel("longterm", (6, 6))
        )
        self.assertTrue(Channel.parse("longterm:6.12").matches(KernelVersion.parse("6.12.115")))
        self.assertFalse(Channel.parse("longterm:6.12").matches(KernelVersion.parse("6.6.161")))
        with self.assertRaises(VersionError):
            Channel.parse("beta")


if __name__ == "__main__":
    unittest.main()

