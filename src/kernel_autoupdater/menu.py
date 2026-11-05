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
    "self_update",
    "status",
    "kernels",
    "doctor",
    "enable",
    "disable",
    "log",
)

_MAIN_ENTRIES: Tuple[Tuple[str, str], ...] = (
    ("检查内核更新", "check"),
    ("更新方式：{method}（切换）", "method"),
    ("预览内核更新（dry-run）", "dry_run"),
    ("立即安装内核更新", "update"),
    ("程序自更新", "self_update"),
    ("设置", "settings"),
    ("环境自检（doctor）", "doctor"),
    ("运行状态", "status"),
    ("已安装内核", "kernels"),
    ("启用定时器", "enable"),
    ("停用定时器", "disable"),
    ("查看日志", "log"),
    ("退出", "quit"),
)

_MAIN_INSTRUCTIONS = "↑/↓ 或 j/k 移动   Enter 选择   q 退出"
_SETTINGS_INSTRUCTIONS = "Enter 编辑/切换   ↑/↓ 或 j/k 移动   Esc/q 返回"
_CHANNEL_INSTRUCTIONS = "Enter 选择   ↑/↓ 或 j/k 移动   Esc/q 返回"


@dataclass(frozen=True)
class _Setting:
    label: str
    section: str
    key: str
    kind: str
    attribute: str


_SETTINGS: Tuple[_Setting, ...] = (
    _Setting("更新方式", "updater", "update_method", "method", "update_method"),
    _Setting("更新通道", "updater", "channel", "channel", "channel"),
    _Setting("自动更新内核", "updater", "auto_update", "bool", "auto_update"),
    _Setting("自动重启", "updater", "auto_reboot", "bool", "auto_reboot"),
    _Setting(
        "重启延迟（分钟）",
        "updater",
        "reboot_delay_minutes",
        "int",
        "reboot_delay_minutes",
    ),
    _Setting("最低剩余空间（GB）", "updater", "min_free_gb", "int", "min_free_gb"),
    _Setting("更新源 URL", "updater", "feed_url", "text", "feed_url"),
    _Setting("更新源超时（秒）", "updater", "feed_timeout", "int", "feed_timeout"),
    _Setting(
        "下载超时（秒）",
        "updater",
        "download_timeout",
        "int",
        "download_timeout",
    ),
    _Setting("工作目录", "updater", "work_dir", "text", "work_dir"),
    _Setting("日志文件（none = 关闭）", "updater", "log_file", "text", "log_file"),
    _Setting("验证 PGP 签名", "source", "verify_pgp", "bool", "verify_pgp"),
    _Setting(
        "允许未校验源码",
        "source",
        "allow_unverified",
        "bool",
        "allow_unverified",
    ),
    _Setting("PGP 公钥环", "source", "pgp_keyring", "text", "pgp_keyring"),
    _Setting("补丁目录", "source", "patches_dir", "text", "patches_dir"),
    _Setting("内核配置来源", "build", "config_source", "text", "config_source"),
    _Setting("本地版本后缀", "build", "localversion", "text", "localversion"),
    _Setting("编译任务数（0 = 自动）", "build", "jobs", "int", "jobs"),
    _Setting(
        "保留编译目录",
        "build",
        "keep_build_dir",
        "bool",
        "keep_build_dir",
    ),
    _Setting(
        "清理发行版签名密钥",
        "build",
        "clear_distro_keys",
        "bool",
        "clear_distro_keys",
    ),
    _Setting(
        "更新 initramfs",
        "install",
        "update_initramfs",
        "bool",
        "update_initramfs",
    ),
    _Setting(
        "更新 bootloader",
        "install",
        "update_bootloader",
        "bool",
        "update_bootloader",
    ),
    _Setting(
        "包管理器（auto/apt/dnf）",
        "packages",
        "manager",
        "text",
        "package_manager",
    ),
    _Setting(
        "内核包（auto = 自动选择）",
        "packages",
        "kernel_packages",
        "text",
        "kernel_packages",
    ),
    _Setting(
        "刷新软件源索引",
        "packages",
        "refresh_package_index",
        "bool",
        "refresh_package_index",
    ),
    _Setting(
        "包更新最低剩余空间（GB）",
        "packages",
        "min_free_gb",
        "int",
        "package_min_free_gb",
    ),
)

_CHANNEL_SETTING = _Setting("更新通道", "updater", "channel", "channel", "channel")
_METHOD_SETTING = _Setting(
    "更新方式", "updater", "update_method", "method", "update_method"
)
_CHANNEL_CHOICES: Tuple[str, ...] = (
    "stable",
    "longterm",
    "mainline",
    "longterm:6.12",
)
_CHANNEL_LABELS = {
    "stable": "稳定版 stable",
    "longterm": "长期支持 longterm",
    "mainline": "主线 mainline",
    "longterm:6.12": "LTS 6.12",
}

_METHOD_CHOICES: Tuple[str, ...] = ("package", "build")
_METHOD_LABELS = {
    "build": "源码编译 build（kernel.org）",
    "package": "发行版预编译包 package（apt/dnf）",
}


def _main_labels(config: Config) -> List[str]:
    return [
        label.format(method=config.update_method) if "{method}" in label else label
        for label, _ in _MAIN_ENTRIES
    ]


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
    return "开" if value else "关"


def _header_lines(config: Config, config_path: Path, version: str) -> List[str]:
    lines = [
        "Kernel AutoUpdater {}".format(version),
        "运行内核: {}".format(running_kernel()),
        "更新方式: {} ({})   自动更新: {}   自动重启: {}".format(
            config.update_method,
            config.channel if config.update_method == "build" else config.package_manager,
            _on_off(config.auto_update),
            _on_off(config.auto_reboot),
        ),
        "配置文件: {}".format(config_path),
    ]
    if _is_root():
        lines.append("权限: root")
    else:
        lines.append("警告: 当前不是 root，安装与保存配置可能失败。")
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
    except (curses.error, UnicodeEncodeError):
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
    except (curses.error, UnicodeEncodeError):
        pass


def _format_setting(config: Config, setting: _Setting) -> str:
    value = getattr(config, setting.attribute)
    if value is None:
        shown = "none"
    elif setting.kind == "bool":
        shown = "[开]" if value else "[关]"
    else:
        shown = str(value)
    return "{:<34} {}".format(setting.label, shown)


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
                print("警告: 动作返回了无效状态。")
            else:
                if code not in (0, 10):
                    print()
                    print("警告: 动作退出状态 {}。".format(code))
        except KeyboardInterrupt:
            code = 130
            print()
            print("动作已中断。")
        except Exception as exc:
            code = 1
            print()
            print("动作失败: {}".format(exc))

        print()
        sys.stdout.flush()
        try:
            input("按 Enter 返回菜单...")
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
        return None, "无法读取 {}: {}".format(path, exc)

    try:
        new_text = set_value(
            previous_text, setting.section, setting.key, value
        )
    except ValueError as exc:
        return None, "无效设置: {}".format(exc)

    try:
        write_config_atomic(path, new_text)
    except OSError as exc:
        return None, "无法保存 {}: {}（请尝试 root）。".format(path, exc)

    try:
        new_config = Config.load(path, explicit=True)
    except ConfigError as exc:
        if _restore_config_file(path, previous_text, existed):
            return (
                None,
                "配置值 {}.{} 无效: {}。已恢复原配置。".format(
                    setting.section, setting.key, exc
                ),
            )
        return (
            None,
            "配置值 {}.{} 无效: {}。原配置文件无法恢复。".format(
                setting.section, setting.key, exc
            ),
        )
    return new_config, "已保存 {}.{}。".format(setting.section, setting.key)


def _validate_setting_value(
    setting: _Setting, value: str
) -> Tuple[Optional[str], Optional[str]]:
    if "\n" in value or "\r" in value:
        return None, "{} 必须是单行文本。".format(setting.label)
    if setting.kind == "int":
        try:
            number = int(value)
        except ValueError:
            return None, "{} 必须是整数。".format(setting.label)
        if number < 0:
            return None, "{} 必须大于或等于 0。".format(setting.label)
        return str(number), None
    if setting.key == "config_source" and not value.strip():
        return None, "内核配置来源不能为空。"
    return value, None


def _channel_selector(
    curses,
    stdscr,
    config: Config,
    config_path: Path,
    version: str,
) -> Tuple[Config, str]:
    custom_label = "自定义..."
    if config.channel in _CHANNEL_CHOICES:
        selected = _CHANNEL_CHOICES.index(config.channel)
    else:
        selected = len(_CHANNEL_CHOICES)
    message = ""

    while True:
        items: List[str] = []
        for choice in _CHANNEL_CHOICES:
            suffix = "  (当前)" if choice == config.channel else ""
            label = _CHANNEL_LABELS.get(choice, choice)
            items.append("{}  [{}]{}".format(label, choice, suffix))
        custom_suffix = (
            "  (当前)" if config.channel not in _CHANNEL_CHOICES else ""
        )
        items.append(custom_label + custom_suffix)

        _draw_screen(
            curses,
            stdscr,
            _header_lines(config, config_path, version),
            "设置 > 更新通道",
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
                message = "已取消编辑。"
                continue

        new_config, result_message = _save_setting(
            config, config_path, _CHANNEL_SETTING, value
        )
        if new_config is None:
            message = result_message
            continue
        return new_config, "更新通道已设为 {}。".format(value)


def _method_selector(
    curses,
    stdscr,
    config: Config,
    config_path: Path,
    version: str,
) -> Tuple[Config, str]:
    selected = (
        _METHOD_CHOICES.index(config.update_method)
        if config.update_method in _METHOD_CHOICES
        else 0
    )
    message = ""

    while True:
        items = []
        for choice in _METHOD_CHOICES:
            suffix = "  (当前)" if choice == config.update_method else ""
            items.append(
                "{}  [{}]{}".format(
                    _METHOD_LABELS.get(choice, choice), choice, suffix
                )
            )

        _draw_screen(
            curses,
            stdscr,
            _header_lines(config, config_path, version),
            "设置 > 更新方式",
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

        value = _METHOD_CHOICES[selected]
        new_config, result_message = _save_setting(
            config, config_path, _METHOD_SETTING, value
        )
        if new_config is None:
            message = result_message
            continue
        return new_config, "更新方式已设为 {}。".format(value)


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
            "设置（修改立即保存）",
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

        if setting.kind == "method":
            config, message = _method_selector(
                curses, stdscr, config, config_path, version
            )
            continue

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
            message = "已取消编辑。"
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

    while True:
        labels = _main_labels(config)
        _draw_screen(
            curses,
            stdscr,
            _header_lines(config, config_path, version),
            "主菜单",
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
        if action_key == "method":
            config, message = _method_selector(
                curses, stdscr, config, config_path, version
            )
            continue
        if action_key == "settings":
            config, message = _settings_loop(
                curses, stdscr, config, config_path, version
            )
            continue

        code = _run_action(curses, stdscr, actions[action_key], config)
        message = (
            ""
            if code in (0, 10)
            else "动作退出状态 {}。".format(code)
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
            "交互菜单需要真实终端 (terminal)，stdin 和 stdout 必须都是 TTY。",
            file=sys.stderr,
        )
        return 2

    try:
        import curses
    except Exception as exc:
        print(
            "交互菜单不可用 (unavailable): {}。请改用 kau 子命令。".format(exc),
            file=sys.stderr,
        )
        return 2

    missing = [key for key in REQUIRED_ACTIONS if key not in actions]
    if missing:
        print(
            "交互菜单配置错误；缺少动作: {}。".format(", ".join(missing)),
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
            "交互菜单无法启动: {}".format(exc),
            file=sys.stderr,
        )
        return 2
