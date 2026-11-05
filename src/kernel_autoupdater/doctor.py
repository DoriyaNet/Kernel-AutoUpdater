"""Host readiness checks."""

from __future__ import annotations

import platform
import os
import sys
from dataclasses import dataclass
from typing import List

from kernel_autoupdater import packages as distro_packages
from kernel_autoupdater.config import Config
from kernel_autoupdater.runner import CommandRunner
from kernel_autoupdater.system import (
    SystemCheckError,
    distro_name,
    find_kernel_config,
    free_gb,
    running_kernel,
    secure_boot_enabled,
)


@dataclass
class Check:
    status: str
    name: str
    detail: str = ""


def _command_check(runner: CommandRunner, name: str, commands: List[str]) -> Check:
    missing = [command for command in commands if not runner.which(command)]
    if missing:
        return Check("FAIL", name, "missing: {}".format(", ".join(missing)))
    return Check("OK", name, ", ".join(commands))


def run_doctor(config: Config, runner: CommandRunner) -> List[Check]:
    package_mode = config.update_method == "package"
    checks: List[Check] = []
    checks.append(
        Check(
            "OK" if platform.system() == "Linux" else "FAIL",
            "platform",
            "{} {} ({})".format(platform.system(), platform.release(), distro_name()),
        )
    )
    checks.append(
        Check(
            "OK" if sys.version_info >= (3, 9) else "FAIL",
            "python",
            platform.python_version(),
        )
    )
    geteuid = getattr(os, "geteuid", None)
    is_root = bool(geteuid and geteuid() == 0)
    checks.append(
        Check("OK" if is_root else "WARN", "root", "running as root" if is_root else "run updates with sudo")
    )
    checks.append(
        Check(
            "OK" if config.source_path and config.source_path.exists() else "WARN",
            "config",
            str(config.source_path) if config.source_path else "using built-in defaults",
        )
    )
    for name, commands, optional in [
        ("systemd", ["systemctl"], True),
    ]:
        check = _command_check(runner, name, commands)
        if check.status == "FAIL" and optional:
            check.status = "WARN"
        checks.append(check)

    package_backend = None
    if package_mode:
        try:
            package_backend = distro_packages.build_backend(config, runner)
            checks.append(
                Check("OK", "package manager", package_backend.describe())
            )
            try:
                update = package_backend.inspect(running_kernel())
                checks.append(
                    Check(
                        "OK",
                        "kernel package",
                        "installed {} / candidate {}".format(
                            update.installed_version or "none",
                            update.candidate_version,
                        ),
                    )
                )
            except distro_packages.PackageError as exc:
                checks.append(Check("WARN", "kernel package", str(exc)))
        except distro_packages.PackageError as exc:
            checks.append(Check("FAIL", "package manager", str(exc)))
    else:
        checks.append(
            Check(
                "OK" if config.patches_dir.is_dir() else "WARN",
                "patches",
                str(config.patches_dir),
            )
        )
        compiler = "gcc" if runner.which("gcc") else "cc"
        checks.append(_command_check(runner, "compiler", [compiler]))
        for name, commands, optional in [
            ("make", ["make"], False),
            ("kernel build tools", ["bc", "flex", "bison", "perl"], False),
            ("archive tools", ["tar"], False),
            ("patch", ["patch"], True),
            ("pahole/BTF", ["pahole"], True),
            ("initramfs", ["dracut"], True),
        ]:
            check = _command_check(runner, name, commands)
            if check.status == "FAIL" and optional:
                check.status = "WARN"
            checks.append(check)

        if runner.which("update-initramfs"):
            checks.append(Check("OK", "initramfs", "update-initramfs"))
        elif runner.which("mkinitcpio"):
            checks.append(
                Check(
                    "WARN",
                    "initramfs",
                    "mkinitcpio needs a custom preset for this kernel",
                )
            )

        try:
            kind, source = find_kernel_config(
                config.config_source, running_kernel()
            )
            checks.append(
                Check(
                    "OK",
                    "kernel config",
                    "{} ({})".format(source or kind, kind),
                )
            )
        except SystemCheckError as exc:
            checks.append(Check("FAIL", "kernel config", str(exc)))

    try:
        available = free_gb(config.work_dir)
        required = (
            config.package_min_free_gb
            if package_mode
            else config.min_free_gb
        )
        status = "OK" if available >= required else "FAIL"
        checks.append(
            Check(
                status,
                "disk space",
                "{:.1f} GiB free, {} GiB required".format(
                    available, required
                ),
            )
        )
    except OSError as exc:
        checks.append(Check("WARN", "disk space", str(exc)))

    secure = secure_boot_enabled(runner)
    if secure is True:
        if package_mode:
            checks.append(
                Check(
                    "OK",
                    "secure boot",
                    "enabled; distro kernel packages are normally signed",
                )
            )
        else:
            checks.append(
                Check(
                    "WARN",
                    "secure boot",
                    "enabled; this tool installs unsigned kernels",
                )
            )
    elif secure is False:
        checks.append(Check("OK", "secure boot", "disabled"))
    else:
        checks.append(Check("WARN", "secure boot", "unknown (mokutil unavailable)"))

    if package_mode and package_backend is not None:
        checks.append(
            Check(
                "OK",
                "package signing",
                "repository metadata and packages are verified by {}".format(
                    config.package_manager
                    if config.package_manager != "auto"
                    else "the package manager"
                ),
            )
        )
    elif config.verify_pgp:
        if runner.which("gpg") and config.pgp_keyring.is_file():
            checks.append(Check("OK", "PGP", str(config.pgp_keyring)))
        else:
            checks.append(
                Check(
                    "FAIL",
                    "PGP",
                    "verify_pgp is enabled but gpg or the keyring is missing",
                )
            )
    else:
        checks.append(
            Check(
                "WARN",
                "PGP",
                "signature verification disabled; SHA-256 over HTTPS is used",
            )
        )

    if runner.which("systemctl"):
        enabled = runner.run(
            ["systemctl", "is-enabled", "kernel-autoupdater.timer"],
            capture=True,
            check=False,
        )
        state = enabled.stdout.strip() or enabled.stderr.strip() or "unknown"
        if "Failed to connect" in state or "System has not been booted" in state:
            state = "unavailable"
        checks.append(
            Check(
                "OK" if state == "enabled" else "WARN",
                "systemd timer",
                state,
            )
        )

    return checks


def print_checks(checks: List[Check]) -> None:
    for check in checks:
        print("[{:<4}] {:<16} {}".format(check.status, check.name, check.detail))

