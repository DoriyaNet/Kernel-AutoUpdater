"""Line-preserving helpers for editing the INI configuration file.

The regular :class:`configparser.ConfigParser` writer rewrites whole files and
drops comments.  The interactive menu wants to keep the operator's comments and
layout, so this module performs small, targeted edits instead.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import List, Optional

DEFAULT_CONFIG_TEXT = """\
[updater]
# build: download kernel.org sources and compile locally
# package: install prebuilt kernels from the distro repository (apt/dnf)
update_method = build
# stable | longterm | mainline | longterm:6.12
channel = stable
feed_url = https://www.kernel.org/releases.json
feed_timeout = 30
download_timeout = 120
work_dir = /var/lib/kernel-autoupdater
log_file = /var/log/kernel-autoupdater.log
auto_update = false
auto_reboot = false
reboot_delay_minutes = 5
min_free_gb = 12

[source]
verify_pgp = false
pgp_keyring = /etc/kernel-autoupdater/kernel.org-keyring.gpg
allow_unverified = false
patches_dir = /etc/kernel-autoupdater/patches

[build]
config_source = auto
localversion = -kau
jobs = 0
keep_build_dir = false
clear_distro_keys = true

[packages]
# auto | apt | dnf
manager = auto
# auto picks linux-image-generic/linux-image-<arch> or kernel
kernel_packages = auto
refresh_package_index = true
min_free_gb = 2

[install]
update_initramfs = true
update_bootloader = true
"""

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SECTION_RE = re.compile(r"^\[([^\]]+)\](?:\s*(?:[#;].*)?)?$")
_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"(?P<separator>[ \t]*[=:][ \t]*)"
    r"(?P<value>.*)$"
)


def _validate_identifier(kind: str, value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ValueError("invalid {} name: {!r}".format(kind, value))


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _section_name(line: str) -> Optional[str]:
    match = _SECTION_RE.match(line.strip())
    if match is None:
        return None
    return match.group(1).strip()


def _find_section(lines: List[str], section: str) -> Optional[int]:
    wanted = section.casefold()
    for index, line in enumerate(lines):
        name = _section_name(line)
        if name is not None and name.casefold() == wanted:
            return index
    return None


def _section_end(lines: List[str], start: int) -> int:
    for index in range(start + 1, len(lines)):
        if _section_name(lines[index]) is not None:
            return index
    return len(lines)


def _key_match(line: str, key: str):
    match = _KEY_RE.match(line)
    if match is None:
        return None
    if match.group("key").casefold() != key.casefold():
        return None
    return match


def _section_indent(lines: List[str], start: int, end: int) -> str:
    for line in lines[start + 1 : end]:
        match = _KEY_RE.match(line)
        if match is not None:
            return match.group("indent")
    return ""


def _join_lines(lines: List[str], trailing_newline: bool) -> str:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    return text


def read_config_text(path: Path) -> str:
    """Return the configuration text, falling back to the default document."""

    try:
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return DEFAULT_CONFIG_TEXT


def get_value(text: str, section: str, key: str) -> Optional[str]:
    """Return a value from *section*/*key*, or ``None`` when it is absent."""

    if not isinstance(section, str) or not isinstance(key, str):
        return None

    lines = _normalize_newlines(text).splitlines()
    start = _find_section(lines, section)
    if start is None:
        return None
    end = _section_end(lines, start)
    for line in lines[start + 1 : end]:
        match = _key_match(line, key)
        if match is not None:
            return match.group("value").strip()
    return None


def set_value(text: str, section: str, key: str, value: str) -> str:
    """Return *text* with one INI value changed.

    Existing comments, blank lines, indentation and unrelated options are left
    untouched.  Missing options are inserted at the end of their section, and a
    missing section is appended to the file.
    """

    _validate_identifier("section", section)
    _validate_identifier("key", key)
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")

    normalized = _normalize_newlines(text)
    trailing_newline = normalized.endswith("\n")
    lines = normalized.splitlines()

    start = _find_section(lines, section)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[{}]".format(section))
        lines.append("{} = {}".format(key, value))
        return _join_lines(lines, trailing_newline)

    end = _section_end(lines, start)
    for index in range(start + 1, end):
        match = _key_match(lines[index], key)
        if match is not None:
            lines[index] = (
                match.group("indent")
                + match.group("key")
                + match.group("separator")
                + value
            )
            return _join_lines(lines, trailing_newline)

    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    indent = _section_indent(lines, start, end)
    lines.insert(insert_at, "{} = {}".format(indent + key, value))
    return _join_lines(lines, trailing_newline)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_config_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* using a temporary file and ``os.replace``."""

    destination = Path(path)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)

    descriptor: Optional[int] = None
    temporary: Optional[str] = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".{}.".format(destination.name),
            suffix=".tmp",
            dir=str(parent),
        )
        if destination.exists():
            try:
                mode = stat.S_IMODE(destination.stat().st_mode)
                os.chmod(temporary, mode)
            except OSError:
                pass
        else:
            try:
                os.chmod(temporary, 0o644)
            except OSError:
                pass

        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline=""
        ) as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass

        os.replace(temporary, str(destination))
        temporary = None
        _fsync_directory(parent)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
