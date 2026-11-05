"""Linux kernel version parsing, comparison and channel selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Optional, Tuple

_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-rc(?P<rc>\d+))?(?=[-+_]|$)",
    re.IGNORECASE,
)
_CHANNEL_RE = re.compile(r"^(stable|longterm|mainline)(?:[:/](?P<series>\d+\.\d+))?$")


class VersionError(ValueError):
    """Raised when a kernel version string cannot be parsed."""


@total_ordering
@dataclass(frozen=True)
class KernelVersion:
    major: int
    minor: int
    patch: int = 0
    rc: Optional[int] = None

    @classmethod
    def parse(cls, value: str) -> "KernelVersion":
        if not value:
            raise VersionError("empty kernel version")
        match = _VERSION_RE.match(value.strip())
        if not match:
            raise VersionError("invalid kernel version: {!r}".format(value))
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch") or 0),
            rc=int(match.group("rc")) if match.group("rc") else None,
        )

    @property
    def base(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def series(self) -> Tuple[int, int]:
        return (self.major, self.minor)

    @property
    def sort_key(self) -> Tuple[int, int, int, int, int]:
        # A final release sorts after its release candidates.
        return (
            self.major,
            self.minor,
            self.patch,
            1 if self.rc is None else 0,
            -(self.rc or 0),
        )

    @property
    def tag(self) -> str:
        """Return the kernel.org git/tarball tag, e.g. 7.4-rc1 or 6.8."""
        if self.patch == 0:
            value = "{}.{}".format(self.major, self.minor)
        else:
            value = "{}.{}.{}".format(self.major, self.minor, self.patch)
        if self.rc is not None:
            value += "-rc{}".format(self.rc)
        return value

    def __str__(self) -> str:
        value = "{}.{}.{}".format(self.major, self.minor, self.patch)
        if self.rc is not None:
            value += "-rc{}".format(self.rc)
        return value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, KernelVersion):
            return NotImplemented
        return self.sort_key < other.sort_key


@dataclass(frozen=True)
class Channel:
    """A release channel such as stable, longterm or longterm:6.12."""

    moniker: str
    series: Optional[Tuple[int, int]] = None

    @classmethod
    def parse(cls, value: str) -> "Channel":
        match = _CHANNEL_RE.match((value or "").strip().lower())
        if not match:
            raise VersionError(
                "无效更新通道 {!r}；请使用 stable、longterm、mainline "
                "或 longterm:<主版本.次版本>".format(value)
            )
        series = None
        if match.group("series"):
            major, minor = match.group("series").split(".", 1)
            series = (int(major), int(minor))
        return cls(moniker=match.group(1), series=series)

    def matches(self, version: KernelVersion) -> bool:
        if self.series is None:
            return True
        return version.series == self.series

    def __str__(self) -> str:
        if self.series is None:
            return self.moniker
        return "{}:{}.{}".format(self.moniker, self.series[0], self.series[1])


def is_newer(candidate: str, current: str) -> bool:
    """Return True when candidate is a newer kernel than current."""
    return KernelVersion.parse(candidate) > KernelVersion.parse(current)


def base_version(value: str) -> str:
    """Return the numeric base version, ignoring distro/localversion suffixes."""
    return str(KernelVersion.parse(value))

