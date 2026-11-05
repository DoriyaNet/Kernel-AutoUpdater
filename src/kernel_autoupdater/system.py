"""Linux host probing helpers."""

from __future__ import annotations

import gzip
import os
import platform
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from kernel_autoupdater.runner import CommandRunner


class SystemCheckError(RuntimeError):
    """Raised when the host is not suitable for an update."""


def is_linux() -> bool:
    return platform.system().lower() == "linux"


def running_kernel() -> str:
    return platform.release()


def distro_name() -> str:
    path = Path("/etc/os-release")
    if not path.exists():
        return "unknown"
    values = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    except OSError:
        return "unknown"
    return values.get("PRETTY_NAME") or values.get("ID") or "unknown"


def cpu_count() -> int:
    return os.cpu_count() or 1


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def free_gb(path: Path) -> float:
    usage = shutil.disk_usage(str(_existing_parent(path)))
    return usage.free / (1024 ** 3)


def find_kernel_config(spec: str, running: str) -> Tuple[str, Optional[Path]]:
    """Resolve a config source.

    Returns ("defconfig", None), ("gzip", path) or ("file", path).
    """
    if spec == "defconfig":
        return ("defconfig", None)

    if spec in ("auto", "running"):
        candidates = [
            Path("/boot/config-{}".format(running)),
            Path("/proc/config.gz"),
            Path("/lib/modules/{}/config".format(running)),
            Path("/usr/lib/modules/{}/config".format(running)),
        ]
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    kind = "gzip" if candidate.name.endswith(".gz") else "file"
                    return (kind, candidate)
            except OSError:
                continue
        raise SystemCheckError(
            "cannot find the running kernel config; looked for /boot/config-{}, "
            "/proc/config.gz and /lib/modules/{}/config. Set build.config_source "
            "to defconfig or a config file explicitly.".format(running, running)
        )

    candidate = Path(os.path.expandvars(os.path.expanduser(spec)))
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise SystemCheckError(
            "kernel config source does not exist or is empty: {}".format(candidate)
        )
    kind = "gzip" if candidate.name.endswith(".gz") else "file"
    return (kind, candidate)


def write_kernel_config(spec: str, running: str, destination: Path) -> str:
    """Copy/decompress the configured kernel config into the source tree."""
    kind, source = find_kernel_config(spec, running)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "defconfig":
        return "make defconfig"
    assert source is not None
    if kind == "gzip":
        with gzip.open(str(source), "rb") as input_handle:
            destination.write_bytes(input_handle.read())
    else:
        shutil.copyfile(str(source), str(destination))
    if destination.stat().st_size < 1024:
        raise SystemCheckError(
            "kernel config {} looks too small to be valid".format(source)
        )
    return str(source)


def secure_boot_enabled(runner: CommandRunner) -> Optional[bool]:
    if runner.which("mokutil"):
        result = runner.run(["mokutil", "--sb-state"], capture=True, check=False)
        text = (result.stdout + "\n" + result.stderr).lower()
        if "secureboot enabled" in text:
            return True
        if "secureboot disabled" in text:
            return False

    efi_vars = Path("/sys/firmware/efi/efivars")
    if efi_vars.is_dir():
        for entry in efi_vars.glob("SecureBoot-*"):
            try:
                data = entry.read_bytes()
            except OSError:
                continue
            if len(data) >= 5:
                return data[4] == 1
    return None


def installed_module_releases() -> List[str]:
    root = Path("/lib/modules")
    if not root.is_dir():
        root = Path("/usr/lib/modules")
    if not root.is_dir():
        return []
    return sorted(
        [entry.name for entry in root.iterdir() if entry.is_dir()],
        reverse=True,
    )


def boot_image_exists(kernel_release: str) -> bool:
    direct = Path("/boot/vmlinuz-{}".format(kernel_release))
    if direct.exists():
        return True
    boot = Path("/boot")
    if boot.is_dir():
        if list(boot.glob("*/{}/vmlinuz".format(kernel_release))):
            return True
        if list(boot.glob("EFI/Linux/*{}*.efi".format(kernel_release))):
            return True
    return False


def initramfs_exists(kernel_release: str) -> bool:
    candidates = [
        Path("/boot/initrd.img-{}".format(kernel_release)),
        Path("/boot/initramfs-{}.img".format(kernel_release)),
    ]
    if any(path.exists() for path in candidates):
        return True
    return False

