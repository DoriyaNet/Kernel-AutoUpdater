"""Distro package repositories for prebuilt kernels.

The source-build path downloads a kernel.org tarball and compiles it on the
host.  This module implements the lighter alternative: let the distribution's
package manager download and install its own kernel packages, including the
distro's initramfs and bootloader integration.
"""

from __future__ import annotations

import logging
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from kernel_autoupdater.config import Config
from kernel_autoupdater.runner import CommandError, CommandRunner
from kernel_autoupdater.versioning import KernelVersion, VersionError

LOGGER = logging.getLogger(__name__)

_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._:-]*$")
_DNF_UPDATE_RE = re.compile(r"^\S+\s+(?P<version>\S+)\s+\S+\s*$")
_APT_IMAGE_PACKAGE_RE = re.compile(
    r"^linux-image-(?:unsigned-)?\d+\.\d+"
)


class PackageError(RuntimeError):
    """Raised when a distro package backend cannot be used."""


@dataclass(frozen=True)
class PackageUpdate:
    manager: str
    packages: Tuple[str, ...]
    installed_version: Optional[str]
    candidate_version: str
    kernel_version: str
    update_available: bool
    pending_reboot: bool = False

    @property
    def description(self) -> str:
        return "{} 软件包仓库（{}）".format(
            self.manager, ", ".join(self.packages)
        )


def parse_package_list(value: str) -> Tuple[str, ...]:
    """Split a comma/whitespace separated package list and validate names."""

    packages = tuple(
        item for item in re.split(r"[\s,]+", value.strip()) if item
    )
    if not packages:
        raise PackageError("内核包列表不能为空")
    invalid = [item for item in packages if not _PACKAGE_RE.match(item)]
    if invalid:
        raise PackageError(
            "内核包名称无效: {}".format(", ".join(invalid))
        )
    return packages


def _read_os_release(path: Optional[Path] = None) -> Dict[str, str]:
    target = path or Path("/etc/os-release")
    values: Dict[str, str] = {}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _distro_ids(values: Dict[str, str]) -> str:
    return " ".join(
        [values.get("ID", ""), values.get("ID_LIKE", "")]
    ).lower()


def _debian_architecture(machine: str) -> str:
    machine = machine.lower()
    return {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "armv7l": "armmp",
        "armv6l": "armmp",
        "i686": "686",
        "i386": "686",
        "ppc64le": "ppc64el",
        "s390x": "s390x",
    }.get(machine, machine)


def _apt_flavor_from_release(release: str) -> Optional[str]:
    """Return the distro kernel flavor encoded in ``uname -r``."""

    value = (release or "").strip()
    if not value or "-" not in value:
        return None
    parts = value.split("-")
    suffix = parts[1:]
    if suffix and re.match(r"^\d", suffix[0]):
        suffix = suffix[1:]
    if not suffix:
        return None
    flavor = "-".join(suffix).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*", flavor):
        return None
    if flavor in ("custom", "kau", "microsoft"):
        return None
    return flavor


def default_packages(
    manager: str,
    os_release: Optional[Dict[str, str]] = None,
    machine: Optional[str] = None,
    running_release: Optional[str] = None,
) -> Tuple[str, ...]:
    values = os_release if os_release is not None else _read_os_release()
    ids = _distro_ids(values)
    arch = _debian_architecture(machine or platform.machine())

    if manager == "apt":
        flavor = _apt_flavor_from_release(running_release or "")
        if flavor:
            return (
                "linux-image-{}".format(flavor),
                "linux-headers-{}".format(flavor),
            )
        if "ubuntu" in ids or not values.get("ID"):
            return ("linux-image-generic", "linux-headers-generic")
        if "debian" in ids:
            return (
                "linux-image-{}".format(arch),
                "linux-headers-{}".format(arch),
            )
        return (
            "linux-image-{}".format(arch),
            "linux-headers-{}".format(arch),
        )
    if manager == "dnf":
        return ("kernel",)
    raise PackageError(
        "不支持的包管理器 {!r}；请使用 apt 或 dnf".format(manager)
    )


def _apt_candidate_available(
    runner: CommandRunner, package: str
) -> bool:
    try:
        result = runner.run(
            ["apt-cache", "policy", package], capture=True, check=False
        )
    except (CommandError, OSError):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if line.strip().startswith("Candidate:"):
            value = line.split(":", 1)[1].strip().lower()
            return bool(value and value != "(none)")
    return False


def detect_manager(
    runner: CommandRunner,
    os_release: Optional[Dict[str, str]] = None,
) -> str:
    values = os_release if os_release is not None else _read_os_release()
    ids = _distro_ids(values)
    has_apt = bool(runner.which("apt-get") and runner.which("apt-cache"))
    has_dnf = bool(runner.which("dnf"))

    if has_apt and ("debian" in ids or "ubuntu" in ids):
        return "apt"
    if has_dnf and any(
        name in ids
        for name in ("fedora", "rhel", "centos", "rocky", "almalinux")
    ):
        return "dnf"
    if has_apt and not has_dnf:
        return "apt"
    if has_dnf and not has_apt:
        return "dnf"
    if has_apt and has_dnf:
        return "apt"
    raise PackageError(
        "无法检测到受支持的包管理器（apt 或 dnf）；"
        "请显式设置 packages.manager"
    )


def _package_kernel_version(version: str) -> str:
    # Debian may use an epoch, e.g. 1:6.8.0-80.80.
    value = version.split(":", 1)[-1] if ":" in version else version
    # Ubuntu meta packages use dots instead of dashes, e.g.
    # 5.15.0.164.159; the kernel base is still 5.15.0.
    match = re.match(r"^(?P<base>\d+\.\d+(?:\.\d+)?)", value)
    if not match:
        raise PackageError(
            "无法从软件包版本 {!r} 解析内核版本".format(
                version
            )
        )
    try:
        return str(KernelVersion.parse(match.group("base")))
    except VersionError as exc:
        raise PackageError(
            "无法从软件包版本 {!r} 解析内核版本".format(
                version
            )
        ) from exc


def _safe_kernel_version(version: str) -> Optional[KernelVersion]:
    value = version.split(":", 1)[-1] if ":" in version else version
    match = re.match(r"^(?P<base>\d+\.\d+(?:\.\d+)?)", value)
    if not match:
        return None
    try:
        return KernelVersion.parse(match.group("base"))
    except VersionError:
        return None


class PackageBackend:
    name = ""

    def __init__(self, runner: CommandRunner, packages: Sequence[str]):
        self.runner = runner
        self.packages = tuple(packages)
        if not self.packages:
            raise PackageError("内核包列表不能为空")

    def describe(self) -> str:
        return "{} ({})".format(self.name, ", ".join(self.packages))

    def install_command(self) -> List[str]:
        raise NotImplementedError

    def refresh(self) -> None:
        raise NotImplementedError

    def inspect(self, current: str) -> PackageUpdate:
        raise NotImplementedError

    def install(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _noninteractive_env() -> Dict[str, str]:
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        return env


class AptBackend(PackageBackend):
    name = "apt"

    def _policy(self, package: str) -> Tuple[Optional[str], str]:
        result = self.runner.run(
            ["apt-cache", "policy", package], capture=True, check=False
        )
        if result.returncode != 0:
            raise PackageError(
                "apt-cache policy 查询 {} 失败: {}".format(
                    package, result.stderr.strip() or result.stdout.strip()
                )
            )

        installed = None
        candidate = None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Installed:"):
                value = stripped.split(":", 1)[1].strip()
                if value and value.lower() != "(none)":
                    installed = value
            elif stripped.startswith("Candidate:"):
                value = stripped.split(":", 1)[1].strip()
                if value and value.lower() != "(none)":
                    candidate = value
        if candidate is None:
            raise PackageError(
                "apt 中没有 {} 的候选版本；请先运行 apt-get update，"
                "或检查 packages.kernel_packages".format(package)
            )
        return installed, candidate

    def _newer(self, candidate: str, installed: Optional[str]) -> bool:
        if installed is None:
            return True
        if candidate == installed:
            return False
        result = self.runner.run(
            ["dpkg", "--compare-versions", candidate, "gt", installed],
            check=False,
        )
        return result.returncode == 0

    def _running_package_version(self, current: str) -> Optional[str]:
        for prefix in ("linux-image-", "linux-image-unsigned-"):
            runtime_package = "{}{}".format(prefix, current)
            result = self.runner.run(
                [
                    "dpkg-query",
                    "-W",
                    "-f=${Status}\\t${Version}\\n",
                    runtime_package,
                ],
                capture=True,
                check=False,
            )
            if result.returncode != 0:
                continue
            text = result.stdout.strip()
            if not text or "install ok installed" not in text:
                continue
            parts = text.split("\t")
            if len(parts) < 2:
                continue
            version = parts[-1].strip()
            if version:
                return version
        return None

    def _newest_installed_image_version(self) -> Optional[str]:
        result = self.runner.run(
            [
                "dpkg-query",
                "-W",
                "-f=${Package}\\t${Status}\\t${Version}\\n",
                "linux-image-*",
            ],
            capture=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            return None

        newest = None
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            package, status, version = parts[0].strip(), parts[1], parts[2].strip()
            if "install ok installed" not in status:
                continue
            if not _APT_IMAGE_PACKAGE_RE.match(package):
                continue
            if not version:
                continue
            if newest is None or self._newer(version, newest):
                newest = version
        return newest

    def inspect(self, current: str) -> PackageUpdate:
        states = []
        for package in self.packages:
            installed, candidate = self._policy(package)
            states.append(
                (package, installed, candidate, self._newer(candidate, installed))
            )

        updated = [state for state in states if state[3]]
        primary = updated[0] if updated else states[0]
        _, installed_version, candidate_version, _ = primary
        update_available = bool(updated)

        pending_reboot = False
        if not update_available:
            running_version = self._running_package_version(current)
            if running_version is not None:
                newest_image = self._newest_installed_image_version()
                pending_reboot = bool(
                    newest_image and self._newer(newest_image, running_version)
                )

        return PackageUpdate(
            manager=self.name,
            packages=self.packages,
            installed_version=installed_version,
            candidate_version=candidate_version,
            kernel_version=_package_kernel_version(candidate_version),
            update_available=update_available,
            pending_reboot=pending_reboot,
        )

    def refresh(self) -> None:
        LOGGER.info("正在刷新 apt 软件源索引")
        self.runner.run(["apt-get", "update", "-q"])

    def install_command(self) -> List[str]:
        return [
            "apt-get",
            "install",
            "-y",
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
            *self.packages,
        ]

    def install(self) -> None:
        LOGGER.info(
            "正在使用 apt 安装预编译内核包: %s",
            ", ".join(self.packages),
        )
        self.runner.run(
            self.install_command(), env=self._noninteractive_env()
        )


class DnfBackend(PackageBackend):
    name = "dnf"

    def _installed_versions(self, package: str) -> List[str]:
        result = self.runner.run(
            [
                "rpm",
                "-q",
                "--qf",
                "%{VERSION}-%{RELEASE}\\n",
                package,
            ],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and "not installed" not in line.lower()
        ]

    def _candidate_version(self, package: str) -> Optional[str]:
        result = self.runner.run(
            [
                "dnf",
                "repoquery",
                "--latest-limit",
                "1",
                "--qf",
                "%{version}-%{release}",
                package,
            ],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and "not installed" not in line.lower()
        ]
        return lines[-1] if lines else None

    def _running_package_version(self, current: str) -> Optional[str]:
        result = self.runner.run(
            [
                "rpm",
                "-q",
                "--qf",
                "%{VERSION}-%{RELEASE}\\n",
                "kernel-{}".format(current),
            ],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        if not value or "not installed" in value.lower():
            return None
        return value.splitlines()[-1].strip() or None

    def inspect(self, current: str) -> PackageUpdate:
        states = []
        for package in self.packages:
            installed = self._installed_versions(package)
            candidate = self._candidate_version(package)
            check = self.runner.run(
                ["dnf", "check-update", "--quiet", package],
                capture=True,
                check=False,
            )
            if check.returncode not in (0, 100):
                raise PackageError(
                    "dnf check-update 查询 {} 失败: {}".format(
                        package, check.stderr.strip() or check.stdout.strip()
                    )
                )
            update_available = check.returncode == 100 or not installed
            if candidate is None:
                for line in reversed(check.stdout.splitlines()):
                    match = _DNF_UPDATE_RE.match(line.strip())
                    if match:
                        candidate = match.group("version")
                        break
            if candidate is None and installed:
                candidate = installed[0]
            if candidate is None:
                raise PackageError(
                    "dnf 中没有 {} 的可用版本".format(package)
                )
            states.append((package, installed, candidate, update_available))

        updated = [state for state in states if state[3]]
        primary = updated[0] if updated else states[0]
        _, installed_versions, candidate_version, _ = primary
        installed_version = (
            max(
                installed_versions,
                key=lambda value: (
                    (
                        _safe_kernel_version(value)
                        or KernelVersion(0, 0, 0)
                    ).sort_key,
                    value,
                ),
            )
            if installed_versions
            else None
        )

        pending_reboot = False
        if not updated:
            running_version = self._running_package_version(current)
            if running_version is not None:
                pending_reboot = bool(
                    installed_version
                    and installed_version != running_version
                )

        return PackageUpdate(
            manager=self.name,
            packages=self.packages,
            installed_version=installed_version,
            candidate_version=candidate_version,
            kernel_version=_package_kernel_version(candidate_version),
            update_available=bool(updated),
            pending_reboot=pending_reboot,
        )

    def refresh(self) -> None:
        LOGGER.info("正在刷新 dnf 软件源元数据")
        self.runner.run(["dnf", "makecache", "-y", "--refresh"])

    def install_command(self) -> List[str]:
        if all(self._installed_versions(package) for package in self.packages):
            return ["dnf", "upgrade", "-y", *self.packages]
        return ["dnf", "install", "-y", *self.packages]

    def install(self) -> None:
        LOGGER.info(
            "正在使用 dnf 安装预编译内核包: %s",
            ", ".join(self.packages),
        )
        self.runner.run(self.install_command())


def build_backend(
    config: Config,
    runner: CommandRunner,
    os_release: Optional[Dict[str, str]] = None,
    running_release: Optional[str] = None,
) -> PackageBackend:
    values = os_release if os_release is not None else _read_os_release()
    manager = (config.package_manager or "auto").strip().lower()
    if manager == "auto":
        manager = detect_manager(runner, values)
    if manager not in ("apt", "dnf"):
        raise PackageError(
            "不支持的包管理器 {!r}；请设置 packages.manager = auto、apt 或 dnf".format(
                manager
            )
        )

    package_setting = (config.kernel_packages or "auto").strip()
    if package_setting.lower() == "auto":
        packages = default_packages(
            manager,
            values,
            running_release=running_release or platform.release(),
        )
        fallback = default_packages(manager, values)
        if (
            manager == "apt"
            and packages != fallback
            and not all(
                _apt_candidate_available(runner, package)
                for package in packages
            )
        ):
            LOGGER.info(
                "当前内核变体（flavor）对应的软件包 %s 在 apt 中不可用，"
                "改用 %s；如需固定运行内核变体，请设置 "
                "packages.kernel_packages",
                ", ".join(packages),
                ", ".join(fallback),
            )
            packages = fallback
    else:
        packages = parse_package_list(package_setting)

    required = (
        ["apt-get", "apt-cache", "dpkg-query", "dpkg"]
        if manager == "apt"
        else ["dnf", "rpm"]
    )
    missing = [command for command in required if not runner.which(command)]
    if missing:
        raise PackageError(
            "包管理器 {} 缺少必需命令: {}".format(
                manager, ", ".join(missing)
            )
        )

    backend_class = AptBackend if manager == "apt" else DnfBackend
    return backend_class(runner, packages)
