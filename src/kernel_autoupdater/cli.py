"""Command line interface for Kernel AutoUpdater."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List, Optional, Sequence

from kernel_autoupdater import __version__, kernelorg
from kernel_autoupdater.builder import (
    CheckResult,
    UpdateError,
    UpdateOptions,
    Updater,
    check_for_update,
)
from kernel_autoupdater.config import Config, ConfigError, load_config
from kernel_autoupdater.doctor import print_checks, run_doctor
from kernel_autoupdater.lock import LockError
from kernel_autoupdater.logging_setup import setup_logging, shutdown_logging
from kernel_autoupdater.runner import CommandError, CommandRunner
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
            "Check, build and install new Linux kernels from kernel.org while "
            "keeping the running kernel as a fallback."
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

    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check", help="check whether a newer kernel is available"
    )
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument(
        "--offline", action="store_true", help="use the cached release feed"
    )
    check.set_defaults(func=cmd_check)

    update = subparsers.add_parser(
        "update", help="download, build and install the latest kernel"
    )
    update.add_argument("--to", metavar="VERSION", help="install a specific version")
    update.add_argument(
        "--dry-run", action="store_true", help="show the plan without changing anything"
    )
    update.add_argument(
        "-y", "--yes", action="store_true", help="do not ask for confirmation"
    )
    update.add_argument(
        "--force",
        action="store_true",
        help="install even if the target is not newer, or Secure Boot is enabled",
    )
    update.add_argument(
        "--reboot", action="store_true", help="reboot after a successful install"
    )
    update.add_argument(
        "--keep-build",
        action="store_true",
        help="reuse an existing extracted source tree",
    )
    update.add_argument(
        "--defconfig",
        action="store_true",
        help="use make defconfig instead of the running kernel config",
    )
    update.add_argument("--jobs", type=int, help="parallel build jobs")
    update.set_defaults(func=cmd_update)

    auto = subparsers.add_parser(
        "auto",
        help="check and update when updater.auto_update is enabled (for systemd)",
    )
    auto.add_argument("--dry-run", action="store_true", help="do not change anything")
    auto.set_defaults(func=cmd_auto)

    status = subparsers.add_parser("status", help="show updater and kernel status")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    kernels = subparsers.add_parser("kernels", help="list installed kernel modules")
    kernels.add_argument("--json", action="store_true", help="machine-readable output")
    kernels.set_defaults(func=cmd_kernels)

    doctor = subparsers.add_parser("doctor", help="check host build prerequisites")
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
        LOGGER.warning("cannot save updater state: %s", exc)


def _fetch_feed(config: Config, offline: bool = False) -> kernelorg.Feed:
    return kernelorg.fetch_feed(
        config.feed_url,
        cache_path=config.feed_cache_path,
        timeout=config.feed_timeout,
        offline=offline,
    )


def _check_result_payload(result: CheckResult) -> dict:
    return {
        "current": result.current,
        "current_base": result.current_base,
        "channel": result.channel,
        "latest": result.latest.version,
        "latest_moniker": result.latest.moniker,
        "latest_released": result.latest.released,
        "source": result.latest.source,
        "update_available": result.update_available,
        "pending_reboot": result.pending_reboot,
        "feed_stale": result.feed_stale,
        "installed_release": result.installed_release,
    }


def cmd_check(args: argparse.Namespace, config: Config) -> int:
    store = _state_store(config)
    state = store.load()
    feed = _fetch_feed(config, offline=args.offline)
    result = check_for_update(config, state, feed=feed)
    state.record_check(result.latest.version)
    _save_state(store, state)

    if result.feed_stale:
        LOGGER.warning("release feed came from cache; it may be out of date")

    if args.json:
        print(json.dumps(_check_result_payload(result), indent=2, sort_keys=True))
    else:
        print("Current kernel : {} ({})".format(result.current, result.current_base))
        print("Channel        : {}".format(result.channel))
        print("Latest release : {}".format(result.latest.version))
        if result.latest.released:
            print("Released       : {}".format(result.latest.released))
        print("Source         : {}".format(result.latest.source))
        print("Update available: {}".format("yes" if result.update_available else "no"))
        if result.pending_reboot:
            print(
                "Pending reboot  : yes ({} is installed)".format(
                    result.installed_release
                )
            )
        if result.installed_release:
            print("Last kau kernel : {}".format(result.installed_release))

    return 10 if result.update_available else 0


def cmd_update(args: argparse.Namespace, config: Config) -> int:
    if args.jobs is not None and args.jobs < 1:
        raise ConfigError("--jobs must be >= 1")
    runner = CommandRunner(dry_run=args.dry_run)
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
    store = _state_store(config)
    state = store.load()
    try:
        feed = _fetch_feed(config)
        result = check_for_update(config, state, feed=feed)
    except Exception as exc:
        state.record_error(str(exc))
        _save_state(store, state)
        raise
    state.record_check(result.latest.version)
    _save_state(store, state)

    if result.pending_reboot:
        LOGGER.info(
            "kernel %s is already installed; reboot to activate it",
            result.installed_release,
        )
        return 0
    if not result.update_available:
        LOGGER.info("no newer kernel than %s", result.current)
        return 0
    if not config.auto_update:
        LOGGER.info(
            "kernel %s is available but updater.auto_update is false",
            result.latest.version,
        )
        return 0

    runner = CommandRunner(dry_run=args.dry_run)
    updater = Updater(config, runner, store)
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
    payload = {
        "running": running_kernel(),
        "channel": config.channel,
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

    print("Running kernel : {}".format(payload["running"]))
    print("Channel        : {}".format(payload["channel"]))
    print("Config         : {}".format(payload["config"] or "built-in defaults"))
    print("Work dir       : {}".format(payload["work_dir"]))
    print("Auto update    : {}".format("yes" if config.auto_update else "no"))
    print("Auto reboot    : {}".format("yes" if config.auto_reboot else "no"))
    if payload["timer"] is not None:
        print("Timer          : {}".format(payload["timer"]))
    print("Last check     : {}".format(state.last_check or "never"))
    if state.last_check_version:
        print("Last seen      : {}".format(state.last_check_version))
    print("Last update    : {}".format(state.last_update or "never"))
    if state.last_error:
        print("Last error     : {}".format(state.last_error))
    if state.installed:
        print("Kernels built by kau:")
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
        print("No kernel module directories found under /lib/modules.")
        return 0
    for row in rows:
        marker = "*" if row["running"] else " "
        origin = " [kau]" if row["built_by_kau"] else ""
        print("{} {}{}".format(marker, row["release"], origin))
    return 0


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    checks = run_doctor(config, CommandRunner())
    print_checks(checks)
    return 1 if any(check.status == "FAIL" for check in checks) else 0


def _require_systemctl(runner: CommandRunner) -> None:
    if not runner.which("systemctl"):
        raise UpdateError("systemctl is not available on this host")


def cmd_enable(args: argparse.Namespace, config: Config) -> int:
    runner = CommandRunner()
    _require_systemctl(runner)
    command = ["systemctl", "enable"]
    if args.now:
        command.append("--now")
    command.append("kernel-autoupdater.timer")
    runner.run(command)
    LOGGER.info("kernel-autoupdater.timer enabled")
    return 0


def cmd_disable(args: argparse.Namespace, config: Config) -> int:
    runner = CommandRunner()
    _require_systemctl(runner)
    runner.run(
        ["systemctl", "disable", "--now", "kernel-autoupdater.timer"]
    )
    LOGGER.info("kernel-autoupdater.timer disabled")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("kau: error: {}".format(exc), file=sys.stderr)
        return 2

    setup_logging(
        verbose=args.verbose,
        quiet=args.quiet,
        log_file=config.log_file,
    )

    try:
        return int(args.func(args, config))
    except KeyboardInterrupt:
        LOGGER.error("interrupted")
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
        VersionError,
        OSError,
    ) as exc:
        LOGGER.error("%s", exc)
        if args.verbose:
            LOGGER.debug("error details", exc_info=True)
        return 1
    except Exception as exc:  # keep systemd logs actionable
        LOGGER.error("unexpected error: %s", exc)
        if args.verbose:
            LOGGER.debug("error details", exc_info=True)
        return 1
    finally:
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())

