import unittest

from kernel_autoupdater.config import Config
from kernel_autoupdater.packages import (
    AptBackend,
    DnfBackend,
    PackageError,
    build_backend,
    default_packages,
    detect_manager,
    parse_package_list,
)
from kernel_autoupdater.runner import CommandResult


def result(stdout="", returncode=0, stderr=""):
    return CommandResult([], returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, handler, available=(), dry_run=False):
        self.handler = handler
        self.available = set(available)
        self.dry_run = dry_run
        self.calls = []

    def which(self, command):
        return "/usr/bin/{}".format(command) if command in self.available else None

    def run(
        self,
        args,
        cwd=None,
        check=True,
        capture=False,
        env=None,
    ):
        command = [str(part) for part in args]
        self.calls.append(command)
        return self.handler(command)


def apt_policy(installed, candidate):
    return result(
        "{}:\n"
        "  Installed: {}\n"
        "  Candidate: {}\n"
        "  Version table:\n"
        " *** {} 500\n".format(
            "linux-image-generic",
            installed or "(none)",
            candidate,
            candidate,
        )
    )


class PackageListTests(unittest.TestCase):
    def test_parse_package_list(self):
        self.assertEqual(
            parse_package_list("linux-image-generic, linux-headers-generic"),
            ("linux-image-generic", "linux-headers-generic"),
        )
        with self.assertRaises(PackageError):
            parse_package_list(" ")
        with self.assertRaises(PackageError):
            parse_package_list("bad/package")

    def test_default_packages(self):
        self.assertEqual(
            default_packages(
                "apt", {"ID": "ubuntu"}, machine="x86_64"
            ),
            ("linux-image-generic", "linux-headers-generic"),
        )
        self.assertEqual(
            default_packages(
                "apt", {"ID": "debian"}, machine="x86_64"
            ),
            ("linux-image-amd64", "linux-headers-amd64"),
        )
        self.assertEqual(
            default_packages("dnf", {"ID": "fedora"}),
            ("kernel",),
        )

    def test_detect_manager_uses_distro_and_commands(self):
        runner = FakeRunner(
            lambda command: result(),
            available=("apt-get", "apt-cache", "dnf"),
        )
        self.assertEqual(detect_manager(runner, {"ID": "debian"}), "apt")
        self.assertEqual(detect_manager(runner, {"ID": "fedora"}), "dnf")

    def test_build_backend_requires_commands(self):
        runner = FakeRunner(lambda command: result(), available=("apt-get",))
        with self.assertRaises(PackageError):
            build_backend(
                Config(package_manager="apt", kernel_packages="kernel"),
                runner,
                os_release={"ID": "debian"},
            )


class AptBackendTests(unittest.TestCase):
    def test_reports_update_and_kernel_base(self):
        def handler(command):
            if command[:3] == ["apt-cache", "policy", "linux-image-generic"]:
                return apt_policy("6.8.0-79.79", "6.8.0-80.80")
            if command[:2] == ["dpkg", "--compare-versions"]:
                return result(returncode=0)
            if command[0] == "dpkg-query":
                return result("install ok installed\t6.8.0-79.79\n")
            return result()

        runner = FakeRunner(
            handler,
            available=("apt-get", "apt-cache", "dpkg-query", "dpkg"),
        )
        backend = AptBackend(runner, ("linux-image-generic",))
        update = backend.inspect("6.8.0-79-generic")
        self.assertTrue(update.update_available)
        self.assertFalse(update.pending_reboot)
        self.assertEqual(update.installed_version, "6.8.0-79.79")
        self.assertEqual(update.candidate_version, "6.8.0-80.80")
        self.assertEqual(update.kernel_version, "6.8.0")

    def test_detects_pending_reboot(self):
        def handler(command):
            if command[:3] == ["apt-cache", "policy", "linux-image-generic"]:
                return apt_policy("6.8.0-80.80", "6.8.0-80.80")
            if command[:2] == ["dpkg", "--compare-versions"]:
                return result(returncode=0)
            if command[0] == "dpkg-query" and command[-1] == "linux-image-*":
                return result(
                    "linux-image-6.8.0-79-generic\tinstall ok installed\t6.8.0-79.79\n"
                    "linux-image-6.8.0-80-generic\tinstall ok installed\t6.8.0-80.80\n"
                )
            if command[0] == "dpkg-query":
                return result("install ok installed\t6.8.0-79.79\n")
            return result()

        runner = FakeRunner(
            handler,
            available=("apt-get", "apt-cache", "dpkg-query", "dpkg"),
        )
        update = AptBackend(runner, ("linux-image-generic",)).inspect(
            "6.8.0-79-generic"
        )
        self.assertFalse(update.update_available)
        self.assertTrue(update.pending_reboot)

    def test_running_newest_kernel_is_not_pending_reboot(self):
        def handler(command):
            if command[:3] == ["apt-cache", "policy", "linux-image-generic"]:
                return apt_policy("6.8.0-80.80", "6.8.0-80.80")
            if command[0] == "dpkg-query" and command[-1] == "linux-image-*":
                return result(
                    "linux-image-6.8.0-80-generic\tinstall ok installed\t6.8.0-80.80\n"
                )
            if command[0] == "dpkg-query":
                return result("install ok installed\t6.8.0-80.80\n")
            return result()

        runner = FakeRunner(
            handler,
            available=("apt-get", "apt-cache", "dpkg-query", "dpkg"),
        )
        update = AptBackend(runner, ("linux-image-generic",)).inspect(
            "6.8.0-80-generic"
        )
        self.assertFalse(update.update_available)
        self.assertFalse(update.pending_reboot)

    def test_parses_ubuntu_dotted_package_version(self):
        def handler(command):
            if command[:3] == ["apt-cache", "policy", "linux-image-generic"]:
                return apt_policy(None, "5.15.0.164.159")
            return result()

        runner = FakeRunner(
            handler,
            available=("apt-get", "apt-cache", "dpkg-query", "dpkg"),
        )
        update = AptBackend(runner, ("linux-image-generic",)).inspect(
            "5.15.0-163-generic"
        )
        self.assertTrue(update.update_available)
        self.assertEqual(update.kernel_version, "5.15.0")

    def test_install_command_is_noninteractive(self):
        backend = AptBackend(
            FakeRunner(lambda command: result()), ("linux-image-generic",)
        )
        command = backend.install_command()
        self.assertIn("apt-get", command)
        self.assertIn("Dpkg::Options::=--force-confdef", command)
        self.assertIn("linux-image-generic", command)

    def test_missing_candidate_is_an_error(self):
        runner = FakeRunner(
            lambda command: result("linux-image-generic:\n  Candidate: (none)\n")
        )
        with self.assertRaises(PackageError):
            AptBackend(runner, ("linux-image-generic",)).inspect(
                "6.8.0-79-generic"
            )


class DnfBackendTests(unittest.TestCase):
    def test_reports_update(self):
        def handler(command):
            if command[:2] == ["rpm", "-q"]:
                return result("6.11.3-300.fc41\n")
            if command[:2] == ["dnf", "repoquery"]:
                return result("6.11.4-301.fc41\n")
            if command[:2] == ["dnf", "check-update"]:
                return result(returncode=100)
            return result()

        runner = FakeRunner(
            handler, available=("dnf", "rpm", "kernel")
        )
        update = DnfBackend(runner, ("kernel",)).inspect(
            "6.11.3-300.fc41.x86_64"
        )
        self.assertTrue(update.update_available)
        self.assertEqual(update.candidate_version, "6.11.4-301.fc41")
        self.assertEqual(update.kernel_version, "6.11.4")

    def test_detects_pending_reboot(self):
        def handler(command):
            if command[:2] == ["rpm", "-q"] and command[-1] == "kernel":
                return result("6.11.4-301.fc41\n6.11.3-300.fc41\n")
            if command[:2] == ["rpm", "-q"]:
                return result("6.11.3-300.fc41\n")
            if command[:2] == ["dnf", "repoquery"]:
                return result("6.11.4-301.fc41\n")
            if command[:2] == ["dnf", "check-update"]:
                return result(returncode=0)
            return result()

        runner = FakeRunner(handler, available=("dnf", "rpm"))
        update = DnfBackend(runner, ("kernel",)).inspect(
            "6.11.3-300.fc41.x86_64"
        )
        self.assertFalse(update.update_available)
        self.assertTrue(update.pending_reboot)


if __name__ == "__main__":
    unittest.main()
