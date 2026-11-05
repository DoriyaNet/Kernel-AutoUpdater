"""Interactive terminal menu for Kernel AutoUpdater.

``curses`` is imported inside :func:`run_menu` on purpose: it is unavailable on
Windows and importing it at module import time would make the rest of the CLI
unusable there.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from kernel_autoupdater.config import Config, ConfigError
from kernel_autoupdater.config_editor import (
    read_config_text,
    set_value,
    write_config_atomic,
)
from kernel_autoupdater.system import running_kernel

Action = Callable[[Config], int]

REQUIRED_ACTIONS: Tuple[str, ...] = (
    "check",
    "dry_run",
    "update",
    "status",
    "kernels",
    "doctor",
    "enable",
    "disable",
    "log",
)

_MAIN_ENTRIES: Tuple[Tuple[str, str], ...] = (
    ("Check for updates", "check"),
    ("Preview update (dry-run)", "dry_run"),
    ("Install update now", "update"),
    ("Settings", "settings"),
    ("Doctor", "doctor"),
    ("Status", "status"),
    ("Installed kernels", "kernels"),
    ("Enable timer", "enable"),
    ("Disable timer", "disable"),
    ("View log", "log"),
    ("Quit", "quit"),
)

_MAIN_INSTRUCTIONS = "Up/Down or j/k: move   Enter: select   q: quit"
_SETTINGS_INSTRUCTIONS = "Enter: edit/toggle   Up/Down or j/k: move   Esc/q: back"
_CHANNEL_INSTRUCTIONS = "Enter: select   Up/Down or j/k: move   Esc/q: back"


@dataclass(frozen=True)
class _Setting:
    label: str
    section: str
    key: str
    kind: str
    attribute: str


_SETTINGS: Tuple[_Setting, ...] = (
    _Setting("Channel", "updater", "channel", "channel", "channel"),
    _Setting("Auto update", "updater", "auto_update", "bool", "auto_update"),
    _Setting("Auto reboot", "updater", "auto_reboot", "bool", "auto_reboot"),
    _Setting(
        "Reboot delay (minutes)",
        "updater",
        "reboot_delay_minutes",
        "int",
        "reboot_delay_minutes",
    ),
    _Setting("Minimum free space (GB)", "updater", "min_free_gb", "int", "min_free_gb"),
    _Setting("Feed URL", "updater", "feed_url", "text", "feed_url"),
    _Setting("Feed timeout (seconds)", "updater", "feed_timeout", "int", "feed_timeout"),
    _Setting(
        "Download timeout (seconds)",
        "updater",
        "download_timeout",
        "int",
        "download_timeout",
    ),
    _Setting("Work directory", "updater", "work_dir", "text", "work_dir"),
    _Setting("Log file (none = off)", "updater", "log_file", "text", "log_file"),
    _Setting("Verify PGP signatures", "source", "verify_pgp", "bool", "verify_pgp"),
    _Setting(
        "Allow unverified sources",
        "source",
        "allow_unverified",
        "bool",
        "allow_unverified",
    ),
    _Setting("PGP keyring", "source", "pgp_keyring", "text", "pgp_keyring"),
    _Setting("Patches directory", "source", "patches_dir", "text", "patches_dir"),
    _Setting("Kernel config source", "build", "config_source", "text", "config_source"),
    _Setting("Local version", "build", "localversion", "text", "localversion"),
    _Setting("Build jobs (0 = auto)", "build", "jobs", "int", "jobs"),
    _Setting(
        "Keep build directory",
        "build",
        "keep_build_dir",
        "bool",
        "keep_build_dir",
    ),
    _Setting(
        "Clear distro signing keys",
        "build",
        "clear_distro_keys",
        "bool",
        "clear_distro_keys",
    ),
    _Setting(
        "Update initramfs",
        "install",
        "update_initramfs",
        "bool",
        "update_initramfs",
    ),
    _Setting(
        "Update bootloader",
        "install",
        "update_bootloader",
        "bool",
        "update_bootloader",
    ),
)

_CHANNEL_SETTING = _Setting("Channel", "updater", "channel", "channel", "channel")
_CHANNEL_CHOICES: Tuple[str, ...] = (
    "stable",
    "longterm",
    "mainline",
    "longterm:6.12",
)


def _stream_is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        return False
    try:
        return geteuid() == 0
    except OSError:
        return False


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _header_lines(config: Config, config_path: Path, version: str) -> List[str]:
    lines = [
        "Kernel AutoUpdater {}".format(version),
        "Running kernel: {}".format(running_kernel()),
        "Channel: {}   Auto update: {}   Auto reboot: {}".format(
            config.channel,
            _on_off(config.auto_update),
            _on_off(config.auto_reboot),
        ),
        "Config: {}".format(config_path),
    ]
    if _is_root():
        lines.append("Privileges: root")
    else:
        lines.append("WARNING: running as non-root; install actions may fail.")
    return lines


def _safe_addstr(
    curses, stdscr, row: int, column: int, text: str, attr: int = 0
) -> None:
    try:
        height, width = stdscr.getmaxyx()
    except curses.error:
        return
    if row < 0 or row >= height or column < 0 or column >= width:
        return
    available = width - column - 1
    if available <= 0:
        return
    try:
        stdscr.addnstr(row, column, text, available, attr)
    except curses.error:
        pass


def _draw_screen(
    curses,
    stdscr,
    header: Sequence[str],
    title: str,
    items: Sequence[str],
    selected: int,
    instructions: str,
    status: str = "",
) -> None:
    try:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
    except curses.error:
        return

    header_budget = max(0, height - 7)
    row = 0
    for line in header[:header_budget]:
        attr = curses.A_BOLD if row == 0 else curses.A_NORMAL
        _safe_addstr(curses, stdscr, row, 0, line, attr)
        row += 1

    if row + 2 < height:
        _safe_addstr(curses, stdscr, row, 0, "-" * max(1, width - 1))
        row += 1
    row += 1

    if row < height:
        _safe_addstr(curses, stdscr, row, 0, title, curses.A_BOLD)
        row += 1

    reserve = 2 if status else 1
    available = max(1, height - row - reserve)
    if items:
        if selected >= available:
            start = selected - available + 1
        else:
            start = 0
        start = max(0, min(start, len(items) - available))
        end = min(len(items), start + available)
    else:
        start = end = 0

    last_item_row = row - 1
    for index in range(start, end):
        label = "> " if index == selected else "  "
        line = label + items[index]
        attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
        if index == selected:
            line = line[: max(0, width - 2)].ljust(max(1, width - 1))
        _safe_addstr(curses, stdscr, row, 0, line, attr)
        last_item_row = row
        row += 1

    if status and height >= 3 and last_item_row < height - 2:
        _safe_addstr(curses, stdscr, height - 2, 0, status)
    if height >= 1 and last_item_row < height - 1:
        _safe_addstr(curses, stdscr, height - 1, 0, instructions)

    try:
        stdscr.refresh()
    except curses.error:
        pass


def _format_setting(config: Config, setting: _Setting) -> str:
    value = getattr(config, setting.attribute)
    if value is None:
        shown = "none"
    elif setting.kind == "bool":
        shown = "[on]" if value else "[off]"
    else:
        shown = str(value)
    return "{:<30} {}".format(setting.label, shown)


def _leave_curses(curses, stdscr) -> None:
    try:
        curses.def_prog_mode()
    except curses.error:
        pass
    try:
        curses.endwin()
    except curses.error:
        pass


def _restore_curses(curses, stdscr) -> None:
    try:
        curses.reset_prog_mode()
    except curses.error:
        pass
    try:
        stdscr.clear()
        stdscr.refresh()
    except curses.error:
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass


def _prompt_value(
    curses, stdscr, label: str, current: str
) -> Optional[str]:
    """Ask for a value outside curses.  An empty answer keeps *current*."""

    _leave_curses(curses, stdscr)
    try:
        print()
        try:
            raw = input("{} [{}]: ".format(label, current))
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        value = raw.strip()
        return current if not value else value
    finally:
        _restore_curses(curses, stdscr)


def _run_action(curses, stdscr, callback: Action, config: Config) -> int:
    code = 0
    _leave_curses(curses, stdscr)
    try:
        try:
            result = callback(config)
            try:
                code = int(result)
            except (TypeError, ValueError):
                code = 1
                print()
                print("Warning: action returned an invalid status.")
            else:
                if code not in (0, 10):
                    print()
                    print("Warning: action exited with status {}.".format(code))
        except KeyboardInterrupt:
            code = 130
            print()
            print("Action interrupted.")
        except Exception as exc:
            code = 1
            print()
            print("Action failed: {}".format(exc))

        print()
        sys.stdout.flush()
        try:
            input("Press Enter to return to the menu...")
        except (EOFError, KeyboardInterrupt):
            pass
    finally:
        _restore_curses(curses, stdscr)
    return code


def _restore_config_file(path: Path, text: str, existed: bool) -> bool:
    try:
        if existed:
            write_config_atomic(path, text)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def _save_setting(
    config: Config, config_path: Path, setting: _Setting, value: str
) -> Tuple[Optional[Config], str]:
    path = Path(config_path)
    existed = path.exists()
    try:
        previous_text = read_config_text(path)
    except OSError as exc:
        return None, "Cannot read {}: {}".format(path, exc)

    try:
        new_text = set_value(
            previous_text, setting.section, setting.key, value
        )
    except ValueError as exc:
        return None, "Invalid setting: {}".format(exc)

    try:
        write_config_atomic(path, new_text)
    except OSError as exc:
        return None, "Cannot save {}: {} (try running as root).".format(path, exc)

    try:
        new_config = Config.load(path, explicit=True)
    except ConfigError as exc:
        if _restore_config_file(path, previous_text, existed):
            return (
                None,
                "Invalid value for {}.{}: {}. Previous configuration restored.".format(
                    setting.section, setting.key, exc
                ),
            )
        return (
            None,
            "Invalid value for {}.{}: {}. The previous file could not be "
            "restored.".format(setting.section, setting.key, exc),
        )
    return new_config, "Saved {}.{}.".format(setting.section, setting.key)


def _validate_setting_value(
    setting: _Setting, value: str
) -> Tuple[Optional[str], Optional[str]]:
    if "\n" in value or "\r" in value:
        return None, "{} must be a single line.".format(setting.label)
    if setting.kind == "int":
        try:
            number = int(value)
        except ValueError:
            return None, "{} must be an integer.".format(setting.label)
        if number < 0:
            return None, "{} must be 0 or greater.".format(setting.label)
        return str(number), None
    if setting.key == "config_source" and not value.strip():
        return None, "Kernel config source must not be empty."
    return value, None


def _channel_selector(
    curses,
    stdscr,
    config: Config,
    config_path: Path,
    version: str,
) -> Tuple[Config, str]:
    custom_label = "Custom..."
    if config.channel in _CHANNEL_CHOICES:
        selected = _CHANNEL_CHOICES.index(config.channel)
    else:
        selected = len(_CHANNEL_CHOICES)
    message = ""

    while True:
        items: List[str] = []
        for choice in _CHANNEL_CHOICES:
            suffix = "  (current)" if choice == config.channel else ""
            items.append(choice + suffix)
        custom_suffix = (
            "  (current)" if config.channel not in _CHANNEL_CHOICES else ""
        )
        items.append(custom_label + custom_suffix)

        _draw_screen(
            curses,
            stdscr,
            _header_lines(config, config_path, version),
            "Settings > Channel",
            items,
            selected,
            _CHANNEL_INSTRUCTIONS,
            message,
        )
        key = stdscr.getch()
        if key == -1:
            return config, ""
        if key == getattr(curses, "KEY_RESIZE", -1):
            message = ""
            continue
        if key in (27, ord("q")):
            return config, ""
        if key in (getattr(curses, "KEY_UP", -1), ord("k")):
            selected = (selected - 1) % len(items)
            message = ""
            continue
        if key in (getattr(curses, "KEY_DOWN", -1), ord("j")):
            selected = (selected + 1) % len(items)
            message = ""
            continue
        if key not in (10, 13, getattr(curses, "KEY_ENTER", -1)):
            continue

        if selected < len(_CHANNEL_CHOICES):
            value = _CHANNEL_CHOICES[selected]
        else:
            value = _prompt_value(
                curses, stdscr, "updater.channel", config.channel
            )
            if value is None:
                message = "Edit cancelled."
                continue

        new_config, result_message = _save_setting(
            config, config_path, _CHANNEL_SETTING, value
        )
        if new_config is None:
            message = result_message
            continue
        return new_config, "Channel set to {}.".format(value)


def _settings_loop(
    curses,
    stdscr,
    config: Config,
    config_path: Path,
    version: str,
) -> Tuple[Config, str]:
    selected = 0
    message = ""

    while True:
        items = [_format_setting(config, setting) for setting in _SETTINGS]
        _draw_screen(
            curses,
            stdscr,
            _header_lines(config, config_path, version),
            "Settings - changes are saved immediately",
            items,
            selected,
            _SETTINGS_INSTRUCTIONS,
            message,
        )
        key = stdscr.getch()
        if key == -1:
            return config, ""
        if key == getattr(curses, "KEY_RESIZE", -1):
            message = ""
            continue
        if key in (27, ord("q")):
            return config, ""
        if key in (getattr(curses, "KEY_UP", -1), ord("k")):
            selected = (selected - 1) % len(items)
            message = ""
            continue
        if key in (getattr(curses, "KEY_DOWN", -1), ord("j")):
            selected = (selected + 1) % len(items)
            message = ""
            continue
        if key not in (10, 13, getattr(curses, "KEY_ENTER", -1)):
            continue

        setting = _SETTINGS[selected]
        message = ""

        if setting.kind == "channel":
            config, message = _channel_selector(
                curses, stdscr, config, config_path, version
            )
            continue

        if setting.kind == "bool":
            current = bool(getattr(config, setting.attribute))
            new_config, message = _save_setting(
                config,
                config_path,
                setting,
                "false" if current else "true",
            )
            if new_config is not None:
                config = new_config
            continue

        current = str(getattr(config, setting.attribute))
        value = _prompt_value(
            curses,
            stdscr,
            "{}.{}".format(setting.section, setting.key),
            current,
        )
        if value is None:
            message = "Edit cancelled."
            continue

        normalized, error = _validate_setting_value(setting, value)
        if error is not None:
            message = error
            continue
        assert normalized is not None

        new_config, message = _save_setting(
            config, config_path, setting, normalized
        )
        if new_config is not None:
            config = new_config


def _run_curses(
    stdscr,
    curses,
    config: Config,
    config_path: Path,
    actions: Dict[str, Action],
    version: str,
) -> int:
    try:
        stdscr.keypad(True)
    except curses.error:
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass

    selected = 0
    message = ""
    labels = [label for label, _ in _MAIN_ENTRIES]

    while True:
        _draw_screen(
            curses,
            stdscr,
            _header_lines(config, config_path, version),
            "Main menu",
            labels,
            selected,
            _MAIN_INSTRUCTIONS,
            message,
        )
        key = stdscr.getch()
        if key == -1:
            return 0
        if key == getattr(curses, "KEY_RESIZE", -1):
            message = ""
            continue
        if key == ord("q"):
            return 0
        if key in (getattr(curses, "KEY_UP", -1), ord("k")):
            selected = (selected - 1) % len(labels)
            message = ""
            continue
        if key in (getattr(curses, "KEY_DOWN", -1), ord("j")):
            selected = (selected + 1) % len(labels)
            message = ""
            continue
        if key not in (10, 13, getattr(curses, "KEY_ENTER", -1)):
            continue

        action_key = _MAIN_ENTRIES[selected][1]
        if action_key == "quit":
            return 0
        if action_key == "settings":
            config, message = _settings_loop(
                curses, stdscr, config, config_path, version
            )
            continue

        code = _run_action(curses, stdscr, actions[action_key], config)
        message = (
            ""
            if code in (0, 10)
            else "Action exited with status {}.".format(code)
        )


def run_menu(
    config: Config,
    config_path: Path,
    actions: Dict[str, Action],
    version: str,
) -> int:
    """Run the interactive menu.

    Returns 0 after a normal quit, 2 when an interactive terminal or the
    ``curses`` module is unavailable, and 130 when interrupted with Ctrl-C.
    """

    if not _stream_is_tty(sys.stdin) or not _stream_is_tty(sys.stdout):
        print(
            "The interactive menu requires a terminal (stdin and stdout must "
            "be a TTY).",
            file=sys.stderr,
        )
        return 2

    try:
        import curses
    except Exception as exc:
        print(
            "The interactive menu is unavailable: {}. Use the kau subcommands "
            "instead.".format(exc),
            file=sys.stderr,
        )
        return 2

    missing = [key for key in REQUIRED_ACTIONS if key not in actions]
    if missing:
        print(
            "The interactive menu is not configured correctly; missing "
            "actions: {}.".format(", ".join(missing)),
            file=sys.stderr,
        )
        return 2

    try:
        return int(
            curses.wrapper(
                _run_curses,
                curses,
                config,
                Path(config_path),
                actions,
                version,
            )
        )
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        print(
            "The interactive menu could not start: {}".format(exc),
            file=sys.stderr,
        )
        return 2
