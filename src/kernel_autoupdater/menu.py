"""Interactive terminal menu for Kernel AutoUpdater.

``curses`` is imported inside :func:`run_menu` on purpose: it is unavailable on
Windows and importing it at module import time would make the rest of the CLI
unusable there.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import queue
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from kernel_autoupdater.config import Config, ConfigError
from kernel_autoupdater.config_editor import (
    read_config_text,
    set_value,
    write_config_atomic,
)
from kernel_autoupdater.runner import command_output_sink
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
    ("预览内核更新（不安装）", "dry_run"),
    ("立即安装内核更新", "update"),
    ("程序自更新", "self_update"),
    ("设置", "settings"),
    ("环境自检", "doctor"),
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
    return "{} {}".format(_pad_display(setting.label, 34), shown)


_ACTION_TITLES = {
    "check": "检查内核更新",
    "dry_run": "预览内核更新",
    "update": "安装内核更新",
    "self_update": "程序自更新",
    "status": "运行状态",
    "kernels": "已安装内核",
    "doctor": "环境自检",
    "enable": "启用定时器",
    "disable": "停用定时器",
    "log": "查看日志",
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_OUTPUT_LIMIT = 5000
_CHOICE_INSTRUCTIONS = "Enter 选择   ↑/↓ 或 j/k 移动   Esc/q 取消"


class _ActionStream(io.TextIOBase):
    """Capture print()/stderr output from a TUI action line by line."""

    def __init__(self, emit: Callable[[str], None]):
        super().__init__()
        self._emit = emit
        self._buffer = ""

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""


class _QueueLogHandler(logging.Handler):
    def __init__(self, emit: Callable[[str], None]):
        super().__init__(logging.INFO)
        self._emit = emit
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        prefix = {
            "WARNING": "警告: ",
            "ERROR": "错误: ",
            "CRITICAL": "严重: ",
        }.get(record.levelname, "")
        self._emit(prefix + message)


@contextlib.contextmanager
def _tui_logging(emit: Callable[[str], None]):
    root = logging.getLogger()
    saved_level = root.level
    console_handlers = []
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            console_handlers.append(handler)
            root.removeHandler(handler)

    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    handler = _QueueLogHandler(emit)
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
        for saved in console_handlers:
            root.addHandler(saved)
        root.setLevel(saved_level)


def _clean_output(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "")


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _wrap_output_lines(lines: Sequence[str], width: int) -> List[str]:
    """Wrap captured output so the output view never hides long lines."""

    usable = max(1, width - 1)
    wrapped: List[str] = []
    for text in lines:
        if not text:
            wrapped.append("")
            continue
        current: List[str] = []
        current_width = 0
        for char in text.expandtabs(8):
            char_width = (
                0
                if unicodedata.combining(char)
                else (
                    2
                    if unicodedata.east_asian_width(char) in ("W", "F")
                    else 1
                )
            )
            if current and current_width + char_width > usable:
                wrapped.append("".join(current))
                current = []
                current_width = 0
            current.append(char)
            current_width += char_width
        wrapped.append("".join(current))
    return wrapped


def _run_action_worker(
    callback: Action,
    config: Config,
    events: "queue.Queue[Tuple[str, object]]",
) -> None:
    def emit(text: str) -> None:
        for line in _clean_output(str(text)).split("\n"):
            events.put(("line", line))

    stream = _ActionStream(emit)
    code = 1
    try:
        with _tui_logging(emit), contextlib.redirect_stdout(
            stream
        ), contextlib.redirect_stderr(stream), command_output_sink(emit):
            try:
                result = callback(config)
                try:
                    code = int(result)
                except (TypeError, ValueError):
                    code = 1
                    emit("警告: 动作返回了无效状态。")
                else:
                    if code not in (0, 10):
                        emit("警告: 动作退出状态 {}。".format(code))
            except KeyboardInterrupt:
                code = 130
                emit("动作已中断。")
            except SystemExit as exc:
                if isinstance(exc.code, int):
                    code = exc.code
                else:
                    code = 1
                emit("动作已退出（状态 {}）。".format(code))
            except Exception as exc:
                code = 1
                emit("动作失败: {}".format(exc))
    finally:
        stream.flush()
        events.put(("done", code))


def _draw_prompt(
    curses,
    stdscr,
    label: str,
    current: str,
    value: str,
) -> None:
    try:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
    except curses.error:
        return
    _safe_addstr(curses, stdscr, 0, 0, "编辑设置", curses.A_BOLD)
    _safe_addstr(curses, stdscr, 1, 0, label)
    _safe_addstr(curses, stdscr, 2, 0, "当前值: {}".format(current or "（空）"))
    if height >= 4:
        available = max(1, width - 3)
        shown = value[-available:] if len(value) > available else value
        _safe_addstr(curses, stdscr, 3, 0, "> " + shown)
        try:
            stdscr.move(3, min(width - 2, 2 + len(shown)))
        except curses.error:
            pass
    if height >= 1:
        _safe_addstr(
            curses,
            stdscr,
            height - 1,
            0,
            "Enter 保存   Esc 取消   留空保持原值",
        )
    try:
        stdscr.refresh()
    except (curses.error, UnicodeEncodeError):
        pass


def _prompt_value(
    curses, stdscr, label: str, current: str
) -> Optional[str]:
    """Edit a value inside curses.  An empty answer keeps *current*."""

    value = ""
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            _draw_prompt(curses, stdscr, label, current, value)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if isinstance(key, str):
                if key in ("\n", "\r"):
                    value = value.strip()
                    return current if not value else value
                if key in ("\x1b", "\x03"):
                    return None
                if key in ("\x7f", "\b"):
                    value = value[:-1]
                elif key == "\x15":
                    value = ""
                elif key.isprintable():
                    value += key
            elif key in (
                getattr(curses, "KEY_BACKSPACE", -1),
                getattr(curses, "KEY_ENTER", -1),
                127,
                8,
            ):
                if key == getattr(curses, "KEY_ENTER", -1):
                    value = value.strip()
                    return current if not value else value
                value = value[:-1]
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _choice_dialog(
    curses,
    stdscr,
    header: Sequence[str],
    title: str,
    items: Sequence[str],
    status: str = "",
) -> Optional[int]:
    selected = 0
    while True:
        _draw_screen(
            curses,
            stdscr,
            header,
            title,
            items,
            selected,
            _CHOICE_INSTRUCTIONS,
            status,
        )
        key = stdscr.getch()
        if key == -1:
            return None
        if key == getattr(curses, "KEY_RESIZE", -1):
            continue
        if key in (27, ord("q")):
            return None
        if key in (getattr(curses, "KEY_UP", -1), ord("k")):
            selected = (selected - 1) % len(items)
            continue
        if key in (getattr(curses, "KEY_DOWN", -1), ord("j")):
            selected = (selected + 1) % len(items)
            continue
        if key in (10, 13, getattr(curses, "KEY_ENTER", -1)):
            return selected


def _output_body_height(stdscr) -> int:
    try:
        height, _ = stdscr.getmaxyx()
    except Exception:
        return 1
    return max(1, height - 7)


def _draw_action_output(
    curses,
    stdscr,
    header: Sequence[str],
    title: str,
    lines: Sequence[str],
    offset: int,
    status: str,
    footer: str,
) -> None:
    try:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
    except curses.error:
        return

    row = 0
    for text in list(header)[:3]:
        _safe_addstr(curses, stdscr, row, 0, text)
        row += 1
    if row < height:
        _safe_addstr(curses, stdscr, row, 0, title, curses.A_BOLD)
        row += 1
    if row < height:
        _safe_addstr(curses, stdscr, row, 0, "-" * max(1, width - 1))
        row += 1

    body = max(1, height - row - 2)
    visible = list(lines[offset : offset + body])
    if not visible:
        visible = ["正在准备..."]
    for text in visible:
        if row >= height - 2:
            break
        _safe_addstr(curses, stdscr, row, 0, text)
        row += 1
    if height >= 2:
        _safe_addstr(curses, stdscr, height - 2, 0, status)
    if height >= 1:
        _safe_addstr(curses, stdscr, height - 1, 0, footer)
    try:
        stdscr.refresh()
    except (curses.error, UnicodeEncodeError):
        pass


def _run_action(
    curses,
    stdscr,
    callback: Action,
    config: Config,
    config_path: Path,
    version: str,
    title: str,
) -> int:
    events: "queue.Queue[Tuple[str, object]]" = queue.Queue()
    worker = threading.Thread(
        target=_run_action_worker,
        args=(callback, config, events),
        daemon=True,
    )
    worker.start()

    lines: List[str] = []
    display_lines: List[str] = []
    display_width = -1
    display_source_count = 0
    offset = 0
    follow = True
    done = False
    code: Optional[int] = None
    notice = ""
    spinner = "|/-\\"
    spinner_index = 0
    stdscr.timeout(100)
    try:
        while True:
            for _ in range(500):
                try:
                    kind, payload = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "line":
                    lines.append(str(payload))
                    if len(lines) > _OUTPUT_LIMIT:
                        dropped = len(lines) - _OUTPUT_LIMIT
                        lines = lines[dropped:]
                        offset = max(0, offset - dropped)
                        display_source_count = -1
                elif kind == "done":
                    done = True
                    code = int(payload)

            try:
                _, width = stdscr.getmaxyx()
            except curses.error:
                width = 80
            if width != display_width:
                display_lines = _wrap_output_lines(lines, width)
                display_width = width
                display_source_count = len(lines)
            elif display_source_count < 0 or len(lines) < display_source_count:
                display_lines = _wrap_output_lines(lines, width)
                display_source_count = len(lines)
            elif len(lines) > display_source_count:
                new_lines = _wrap_output_lines(
                    lines[display_source_count:], width
                )
                display_lines.extend(new_lines)
                display_source_count = len(lines)
            body = _output_body_height(stdscr)
            max_offset = max(0, len(display_lines) - body)
            if follow:
                offset = max_offset
            else:
                offset = max(0, min(offset, max_offset))

            header = _header_lines(config, config_path, version)
            if done:
                if code == 0:
                    status = "已完成（退出码 0）"
                elif code == 10:
                    status = "已完成：有可用更新（退出码 10）"
                else:
                    status = "执行失败（退出码 {}）".format(code)
                footer = "↑/↓ PgUp/PgDn 滚动   Enter/Esc/q 返回"
            else:
                spinner_index = (spinner_index + 1) % len(spinner)
                status = "{} 正在执行...".format(spinner[spinner_index])
                if notice:
                    status += "  " + notice
                footer = "↑/↓ PgUp/PgDn 滚动   任务完成后可返回"

            _draw_action_output(
                curses,
                stdscr,
                header,
                title,
                display_lines,
                offset,
                status,
                footer,
            )

            key = stdscr.getch()
            if key == -1:
                continue
            if key == getattr(curses, "KEY_RESIZE", -1):
                continue
            if key in (
                getattr(curses, "KEY_UP", -1),
                ord("k"),
            ):
                follow = False
                offset = max(0, offset - 1)
                notice = ""
                continue
            if key in (
                getattr(curses, "KEY_DOWN", -1),
                ord("j"),
            ):
                follow = False
                offset = min(max_offset, offset + 1)
                notice = ""
                continue
            if key == getattr(curses, "KEY_PPAGE", -1):
                follow = False
                offset = max(0, offset - body)
                notice = ""
                continue
            if key == getattr(curses, "KEY_NPAGE", -1):
                follow = False
                offset = min(max_offset, offset + body)
                notice = ""
                continue
            if key == getattr(curses, "KEY_HOME", -1):
                follow = False
                offset = 0
                notice = ""
                continue
            if key == getattr(curses, "KEY_END", -1):
                follow = True
                notice = ""
                continue
            if key in (10, 13, 27, getattr(curses, "KEY_ENTER", -1), ord("q")):
                if done:
                    return code if code is not None else 1
                notice = "任务仍在运行，请等待完成。"
    finally:
        stdscr.timeout(-1)


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

    notes: List[str] = []
    try:
        with _tui_logging(notes.append):
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
    message = "已保存 {}.{}。".format(setting.section, setting.key)
    if notes:
        message += " " + " ".join(notes)
    return new_config, message


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

        if action_key == "update":
            choice = _choice_dialog(
                curses,
                stdscr,
                _header_lines(config, config_path, version),
                "立即安装内核更新",
                ["安装，完成后不重启", "安装后重启", "取消"],
                "请选择安装选项",
            )
            if choice is None or choice == 2:
                continue
            callback = (
                actions.get("update_reboot", actions["update"])
                if choice == 1
                else actions["update"]
            )
        elif action_key == "self_update":
            choice = _choice_dialog(
                curses,
                stdscr,
                _header_lines(config, config_path, version),
                "程序自更新",
                ["从 GitHub 重新安装 kau", "取消"],
                "更新完成后需要重新启动 kau",
            )
            if choice != 0:
                continue
            callback = actions["self_update"]
        else:
            callback = actions[action_key]

        code = _run_action(
            curses,
            stdscr,
            callback,
            config,
            config_path,
            version,
            _ACTION_TITLES.get(action_key, action_key),
        )
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
            "交互菜单需要真实终端，标准输入和标准输出都必须连接到 TTY。",
            file=sys.stderr,
        )
        return 2

    try:
        import curses
    except Exception as exc:
        print(
            "交互菜单不可用: {}。请改用 kau 子命令。".format(exc),
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
