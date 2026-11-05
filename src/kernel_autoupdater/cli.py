"""Command line interface for Kernel AutoUpdater."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Sequence

from kernel_autoupdater import __version__, kernelorg
from kernel_autoupdater import packages as distro_packages
from kernel_autoupdater.builder import (
    CheckResult,
    UpdateError,
    UpdateOptions,
    Updater,
    check_for_update,
    check_package_update,
)
from kernel_autoupdater.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    load_config,
)
from kernel_autoupdater.doctor import print_checks, run_doctor
from kernel_autoupdater.lock import LockError
from kernel_autoupdater.logging_setup import setup_logging, shutdown_logging
from kernel_autoupdater.runner import CommandError, CommandRunner
from kernel_autoupdater.selfupdate import (
    DEFAULT_REF,
    SelfUpdateError,
    run_self_update,
)
from kernel_autoupdater.state import State, StateStore
from kernel_autoupdater.system import (
    SystemCheckError,
    installed_module_releases,
    running_kernel,
)
from kernel_autoupdater.versioning import KernelVersion, VersionError

LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kau",
        description=(
            "Check, build or install new Linux kernels from kernel.org or "
            "distro package repositories while keeping the running kernel "
            "as a fallback."
        ),
    )
    parser.add_argument(
        "--config",
        help="path to config.ini (default: /etc/kernel-autoupdater/config.ini)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase log verbosity (repeat for debug)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only show errors")
    parser.add_argument(
        "--version", action="version", version="%(prog)s {}".format(__version__)
    )

    subparsers = parser.add_subparsers(dest="command")

    menu = subparsers.add_parser(
        "menu",
        aliases=["tui", "ui"],
        help="open the interactive terminal menu",
    )
    menu.set_defaults(func=cmd_menu)

    check = subparsers.add_parser(
        "check", help="check whether a newer kernel is available"
    )
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument(
        "--offline",
        action="store_true",
        help="use the cached release feed (build mode)",
    )
    check.add_argument(
        "--method",
        choices=("build", "package"),
        help="override updater.update_method for this run",
    )
    check.set_defaults(func=cmd_check)

    update = subparsers.add_parser(
        "update", help="install the latest kernel (source build or package)"
    )
    update.add_argument(
        "--to",
        metavar="VERSION",
        help="install a specific kernel.org version (build mode only)",
    )
    update.add_argument(
        "--dry-run", action="store_true", help="show the plan without changing anything"
    )
    update.add_argument(
        "-y", "--yes", action="store_true", help="do not ask for confirmation"
    )
    update.add_argument(
        "--force",
        action="store_true",
        help=(
            "install even if the target is not newer, or bypass the build-mode "
            "Secure Boot check"
        ),
    )
    update.add_argument(
        "--reboot", action="store_true", help="reboot after a successful install"
    )
    update.add_argument(
        "--keep-build",
        action="store_true",
        help="reuse an existing extracted source tree (build mode only)",
    )
    update.add_argument(
        "--defconfig",
        action="store_true",
        help="use make defconfig instead of the running kernel config (build mode only)",
    )
    update.add_argument(
        "--jobs", type=int, help="parallel build jobs (build mode only)"
    )
    update.add_argument(
        "--method",
        choices=("build", "package"),
        help="override updater.update_method for this run",
    )
    update.set_defaults(func=cmd_update)

    auto = subparsers.add_parser(
        "auto",
        help="check and update when updater.auto_update is enabled (for systemd)",
    )
    auto.add_argument("--dry-run", action="store_true", help="do not change anything")
    auto.add_argument(
        "--method",
        choices=("build", "package"),
        help="override updater.update_method for this run",
    )
    auto.set_defaults(func=cmd_auto)

    self_update = subparsers.add_parser(
        "self-update",
        aliases=["update-self", "upgrade"],
        help="update the kau program itself from GitHub",
    )
    self_update.add_argument(
        "--check", action="store_true", help="only check the remote version"
    )
    self_update.add_argument(
        "--force", action="store_true", help="reinstall even if versions match"
    )
    self_update.add_argument(
        "-y", "--yes", action="store_true", help="do not ask for confirmation"
    )
    self_update.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help="branch or tag to install (default: master)",
    )
    self_update.set_defaults(func=cmd_self_update)

    status = subparsers.add_parser("status", help="show updater and kernel status")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    kernels = subparsers.add_parser("kernels", help="list installed kernel modules")
    kernels.add_argument("--json", action="store_true", help="machine-readable output")
    kernels.set_defaults(func=cmd_kernels)

    doctor = subparsers.add_parser("doctor", help="check host prerequisites")
    doctor.add_argument(
        "--method",
        choices=("build", "package"),
        help="override updater.update_method for this run",
    )
    doctor.set_defaults(func=cmd_doctor)

    enable = subparsers.add_parser("enable", help="enable the systemd update timer")
    enable.add_argument("--now", action="store_true", help="start the timer immediately")
    enable.set_defaults(func=cmd_enable)

    disable = subparsers.add_parser("disable", help="disable and stop the systemd timer")
    disable.set_defaults(func=cmd_disable)

    return parser


def _state_store(config: Config) -> StateStore:
    return StateStore(config.state_path)


def _save_state(store: StateStore, state: State) -> None:
    try:
        store.save(state)
    except OSError as exc:
        LOGGER.warning("无法保存更新状态: %s", exc)


def _fetch_feed(config: Config, offline: bool = False) -> kernelorg.Feed:
    return kernelorg.fetch_feed(
        config.feed_url,
        cache_path=config.feed_cache_path,
        timeout=config.feed_timeout,
        offline=offline,
    )


def _with_method(config: Config, method: Optional[str]) -> Config:
    if method and method != config.update_method:
        return replace(config, update_method=method)
    return config


def _check_result_payload(result: CheckResult) -> dict:
    payload = {
        "current": result.current,
        "current_base": result.current_base,
        "channel": result.channel,
        "method": result.method,
        "latest": result.latest_version,
        "latest_moniker": result.latest.moniker,
        "latest_released": result.latest.released,
        "source": result.latest.source,
        "update_available": result.update_available,
        "pending_reboot": result.pending_reboot,
        "feed_stale": result.feed_stale,
        "installed_release": result.installed_release,
    }
    if result.package is not None:
        payload.update(
            {
                "package_manager": result.package.manager,
                "package_names": list(result.package.packages),
                "package_installed": result.package.installed_version,
                "package_candidate": result.package.candidate_version,
                "package_kernel_base": result.package.kernel_version,
            }
        )
    return payload


def cmd_check(args: argparse.Namespace, config: Config) -> int:
    config = _with_method(config, getattr(args, "method", None))
    store = _state_store(config)
    state = store.load()
    if config.update_method == "package":
        result = check_package_update(config, state, CommandRunner())
    else:
        feed = _fetch_feed(config, offline=args.offline)
        result = check_for_update(config, state, feed=feed)
    state.record_check(result.latest_version)
    _save_state(store, state)

    if result.feed_stale:
        LOGGER.warning("发布信息来自缓存，可能已过期")

    if args.json:
        print(json.dumps(_check_result_payload(result), indent=2, sort_keys=True))
    else:
        print("当前内核 : {} ({})".format(result.current, result.current_base))
        if result.method == "package" and result.package is not None:
            print(
                "更新方式 : package ({})".format(result.package.manager)
            )
            print(
                "内核包   : {}".format(
                    ", ".join(result.package.packages)
                )
            )
            print(
                "已安装包 : {}".format(
                    result.package.installed_version or "未安装"
                )
            )
            print(
                "候选包   : {}".format(result.package.candidate_version)
            )
            print(
                "内核基线 : {}".format(result.package.kernel_version)
            )
        else:
            print("更新方式 : 源码编译 build")
            print("更新通道 : {}".format(result.channel))
        latest_label = (
            "最新包  "
            if result.method == "package"
            else "最新版本"
        )
        print("{} : {}".format(latest_label, result.latest_version))
        if result.latest.released and result.method != "package":
            print("发布时间 : {}".format(result.latest.released))
        print("来源     : {}".format(result.latest.source))
        print("有可用更新: {}".format("是" if result.update_available else "否"))
        if result.pending_reboot:
            print(
                "等待重启: 是（{} 已安装）".format(
                    result.installed_release
                )
            )
        if result.installed_release:
            print("上次安装内核: {}".format(result.installed_release))

    return 10 if result.update_available else 0


def cmd_update(args: argparse.Namespace, config: Config) -> int:
    config = _with_method(config, getattr(args, "method", None))
    if args.jobs is not None and args.jobs < 1:
        raise ConfigError("--jobs must be >= 1")
    runner = CommandRunner(
        dry_run=args.dry_run and config.update_method != "package"
    )
    updater = Updater(config, runner, _state_store(config))
    options = UpdateOptions(
        target=args.to,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        force=args.force,
        reboot=args.reboot,
        keep_build=args.keep_build,
        defconfig=args.defconfig,
        jobs=args.jobs,
    )
    updater.execute(options)
    return 0


def cmd_auto(args: argparse.Namespace, config: Config) -> int:
    config = _with_method(config, getattr(args, "method", None))
    store = _state_store(config)
    state = store.load()
    try:
        if config.update_method == "package":
            result = check_package_update(
                config,
                state,
                CommandRunner(),
                refresh=bool(
                    config.refresh_package_index
                    and getattr(os, "geteuid", lambda: 1)() == 0
                ),
            )
            feed = None
        else:
            feed = _fetch_feed(config)
            result = check_for_update(config, state, feed=feed)
    except Exception as exc:
        state.record_error(str(exc))
        _save_state(store, state)
        raise
    state.record_check(result.latest_version)
    _save_state(store, state)

    if result.pending_reboot:
        LOGGER.info(
            "内核 %s 已安装，请重启以启用",
            result.installed_release,
        )
        return 0
    if not result.update_available:
        LOGGER.info("没有比 %s 更新的内核", result.current)
        return 0
    if not config.auto_update:
        LOGGER.info(
            "发现内核 %s，但 updater.auto_update 为 false",
            result.latest_version,
        )
        return 0

    runner = CommandRunner(
        dry_run=args.dry_run and config.update_method != "package"
    )
    updater_config = config
    if config.update_method == "package" and not args.dry_run:
        # cmd_auto already refreshed the package index for the check above.
        updater_config = replace(config, refresh_package_index=False)
    updater = Updater(updater_config, runner, store)
    updater.execute(
        UpdateOptions(
            dry_run=args.dry_run,
            assume_yes=True,
            reboot=config.auto_reboot,
        ),
        release=result.latest,
        feed=feed,
    )
    return 0


def cmd_self_update(args: argparse.Namespace, config: Config) -> int:
    return run_self_update(
        ref=args.ref,
        check_only=args.check,
        force=args.force,
        assume_yes=args.yes,
    )


def _systemd_state(runner: CommandRunner, unit: str) -> Optional[str]:
    if not runner.which("systemctl"):
        return None
    result = runner.run(
        ["systemctl", "is-active", unit], capture=True, check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or "unknown"


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    store = _state_store(config)
    state = store.load()
    runner = CommandRunner()
    package_manager = config.package_manager
    if config.update_method == "package" and package_manager == "auto":
        try:
            package_manager = distro_packages.detect_manager(runner)
        except distro_packages.PackageError:
            pass
    payload = {
        "running": running_kernel(),
        "channel": config.channel,
        "update_method": config.update_method,
        "package_manager": package_manager,
        "package_manager_config": config.package_manager,
        "kernel_packages": config.kernel_packages,
        "config": str(config.source_path) if config.source_path else None,
        "work_dir": str(config.work_dir),
        "auto_update": config.auto_update,
        "auto_reboot": config.auto_reboot,
        "timer": _systemd_state(runner, "kernel-autoupdater.timer"),
        "last_check": state.last_check,
        "last_check_version": state.last_check_version,
        "last_update": state.last_update,
        "running_at_update": state.running_at_update,
        "last_error": state.last_error,
        "installed": [
            {
                "version": item.version,
                "release": item.release,
                "installed_at": item.installed_at,
                "source": item.source,
            }
            for item in state.installed
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("运行内核 : {}".format(payload["running"]))
    print("更新方式 : {}".format(payload["update_method"]))
    if config.update_method == "build":
        print("更新通道 : {}".format(payload["channel"]))
    else:
        print("包管理器 : {}".format(payload["package_manager"]))
        print("内核包   : {}".format(payload["kernel_packages"]))
    print("配置文件 : {}".format(payload["config"] or "内置默认值"))
    print("工作目录 : {}".format(payload["work_dir"]))
    print("自动更新 : {}".format("是" if config.auto_update else "否"))
    print("自动重启 : {}".format("是" if config.auto_reboot else "否"))
    if payload["timer"] is not None:
        timer = {
            "active": "运行中",
            "inactive": "未运行",
            "failed": "失败",
        }.get(payload["timer"], payload["timer"])
        print("定时器   : {}".format(timer))
    print("上次检查 : {}".format(state.last_check or "从未"))
    if state.last_check_version:
        print("上次发现 : {}".format(state.last_check_version))
    print("上次更新 : {}".format(state.last_update or "从未"))
    if state.last_error:
        print("上次错误 : {}".format(state.last_error))
    if state.installed:
        print("由 kau 安装的内核:")
        for item in state.installed:
            print(
                "  {} ({}) at {}".format(
                    item.release, item.version, item.installed_at
                )
            )
    return 0


def _sort_releases(releases: List[str]) -> List[str]:
    def key(value: str):
        try:
            return (0, KernelVersion.parse(value).sort_key)
        except VersionError:
            return (1, value)

    return sorted(releases, key=key, reverse=True)


def cmd_kernels(args: argparse.Namespace, config: Config) -> int:
    state = _state_store(config).load()
    built = {item.release: item for item in state.installed}
    current = running_kernel()
    rows = []
    for release in _sort_releases(installed_module_releases()):
        rows.append(
            {
                "release": release,
                "running": release == current,
                "built_by_kau": release in built,
                "installed_at": built[release].installed_at if release in built else None,
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("在 /lib/modules 下没有找到内核模块目录。")
        return 0
    for row in rows:
        marker = "*" if row["running"] else " "
        origin = " [kau]" if row["built_by_kau"] else ""
        print("{} {}{}".format(marker, row["release"], origin))
    return 0


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    config = _with_method(config, getattr(args, "method", None))
    checks = run_doctor(config, CommandRunner())
    print_checks(checks)
    return 1 if any(check.status == "FAIL" for check in checks) else 0


def _menu_check(config: Config) -> int:
    return cmd_check(SimpleNamespace(json=False, offline=False), config)


def _menu_dry_run(config: Config) -> int:
    return cmd_update(
        SimpleNamespace(
            to=None,
            dry_run=True,
            yes=True,
            force=False,
            reboot=False,
            keep_build=False,
            defconfig=False,
            jobs=None,
        ),
        config,
    )


def _menu_self_update(config: Config) -> int:
    return run_self_update(
        ref=DEFAULT_REF,
        check_only=False,
        force=False,
        assume_yes=True,
    )


def _menu_update(config: Config) -> int:
    return _menu_update_with_reboot(config, reboot=False)


def _menu_update_reboot(config: Config) -> int:
    return _menu_update_with_reboot(config, reboot=True)


def _menu_update_with_reboot(config: Config, reboot: bool) -> int:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and geteuid() != 0:
        print("安装内核需要 root 权限，请使用 sudo kau 运行。")
        return 1
    print("当前内核: {}".format(running_kernel()))
    print("更新方式: {}".format(config.update_method))
    if config.update_method == "build":
        print("更新通道: {}".format(config.channel))
    else:
        print("包管理器: {}".format(config.package_manager))
        print("内核包: {}".format(config.kernel_packages))
    print("安装后重启: {}".format("是" if reboot else "否"))
    return cmd_update(
        SimpleNamespace(
            to=None,
            dry_run=False,
            yes=True,
            force=False,
            reboot=reboot,
            keep_build=False,
            defconfig=False,
            jobs=None,
        ),
        config,
    )


def _menu_status(config: Config) -> int:
    return cmd_status(SimpleNamespace(json=False), config)


def _menu_kernels(config: Config) -> int:
    return cmd_kernels(SimpleNamespace(json=False), config)


def _menu_doctor(config: Config) -> int:
    return cmd_doctor(SimpleNamespace(), config)


def _menu_enable(config: Config) -> int:
    return cmd_enable(SimpleNamespace(now=True), config)


def _menu_disable(config: Config) -> int:
    return cmd_disable(SimpleNamespace(), config)


def _menu_log(config: Config) -> int:
    if config.log_file:
        try:
            if Path(config.log_file).is_file():
                print("日志文件: {}".format(config.log_file))
                print()
                lines = Path(config.log_file).read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                print("\n".join(lines[-80:]))
                return 0
        except OSError as exc:
            print("无法读取 {}: {}".format(config.log_file, exc))

    if shutil.which("journalctl"):
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                "kernel-autoupdater.service",
                "-n",
                "80",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        output = "\n".join(
            part for part in (result.stdout, result.stderr) if part
        )
        output = "\n".join(
            line
            for line in output.splitlines()
            if line.strip()
            not in ("-- No entries --", "No journal files were found.")
        ).strip()
        if output:
            print(output)
        else:
            print("没有可显示的日志记录。")
        return result.returncode
    print("没有可用的日志文件或 journalctl。")
    return 0


def cmd_menu(args: argparse.Namespace, config: Config) -> int:
    from kernel_autoupdater.menu import run_menu

    actions = {
        "check": _menu_check,
        "dry_run": _menu_dry_run,
        "update": _menu_update,
        "update_reboot": _menu_update_reboot,
        "self_update": _menu_self_update,
        "status": _menu_status,
        "kernels": _menu_kernels,
        "doctor": _menu_doctor,
        "enable": _menu_enable,
        "disable": _menu_disable,
        "log": _menu_log,
    }
    path = config.source_path or DEFAULT_CONFIG_PATH
    return run_menu(config, path, actions, __version__)


def _require_systemctl(runner: CommandRunner) -> None:
    if not runner.which("systemctl"):
        raise UpdateError("当前系统没有 systemctl，无法管理定时器")


def _require_timer_unit(runner: CommandRunner) -> None:
    result = runner.run(
        ["systemctl", "cat", "kernel-autoupdater.timer"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        raise UpdateError(
            "未找到 kernel-autoupdater.timer；"
            "请重新运行安装脚本，或确认 systemd 可用"
        )


def cmd_enable(args: argparse.Namespace, config: Config) -> int:
    runner = CommandRunner()
    _require_systemctl(runner)
    _require_timer_unit(runner)
    command = ["systemctl", "enable"]
    if args.now:
        command.append("--now")
    command.append("kernel-autoupdater.timer")
    runner.run(command)
    LOGGER.info("已启用 kernel-autoupdater.timer")
    return 0


def cmd_disable(args: argparse.Namespace, config: Config) -> int:
    runner = CommandRunner()
    _require_systemctl(runner)
    _require_timer_unit(runner)
    runner.run(
        ["systemctl", "disable", "--now", "kernel-autoupdater.timer"]
    )
    LOGGER.info("已停用 kernel-autoupdater.timer")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        if sys.stdin.isatty() and sys.stdout.isatty():
            args.command = "menu"
            args.func = cmd_menu
        else:
            parser.print_help()
            return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("kau: 错误: {}".format(exc), file=sys.stderr)
        return 2

    setup_logging(
        verbose=args.verbose,
        quiet=args.quiet,
        log_file=config.log_file,
    )

    try:
        return int(args.func(args, config))
    except KeyboardInterrupt:
        LOGGER.error("操作已中断")
        return 130
    except (
        UpdateError,
        ConfigError,
        SystemCheckError,
        CommandError,
        LockError,
        kernelorg.FeedError,
        kernelorg.ReleaseNotFoundError,
        kernelorg.DownloadError,
        distro_packages.PackageError,
        SelfUpdateError,
        VersionError,
        OSError,
    ) as exc:
        LOGGER.error("%s", exc)
        if args.verbose:
            LOGGER.debug("错误详情", exc_info=True)
        return 1
    except Exception as exc:  # keep systemd logs actionable
        LOGGER.error("发生未预期的错误: %s", exc)
        if args.verbose:
            LOGGER.debug("错误详情", exc_info=True)
        return 1
    finally:
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())

