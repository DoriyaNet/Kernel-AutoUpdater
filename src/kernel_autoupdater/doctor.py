"""Host readiness checks."""

from __future__ import annotations

import platform
import os
import sys
import unicodedata
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
        return Check("FAIL", name, "缺少: {}".format(", ".join(missing)))
    return Check("OK", name, ", ".join(commands))


def run_doctor(config: Config, runner: CommandRunner) -> List[Check]:
    package_mode = config.update_method == "package"
    checks: List[Check] = []
    checks.append(
        Check(
            "OK" if platform.system() == "Linux" else "FAIL",
            "平台",
            "{} {} ({})".format(platform.system(), platform.release(), distro_name()),
        )
    )
    checks.append(
        Check(
            "OK" if sys.version_info >= (3, 9) else "FAIL",
            "Python",
            platform.python_version(),
        )
    )
    geteuid = getattr(os, "geteuid", None)
    is_root = bool(geteuid and geteuid() == 0)
    checks.append(
        Check(
            "OK" if is_root else "WARN",
            "权限",
            "当前以 root 运行" if is_root else "请使用 sudo 运行更新",
        )
    )
    checks.append(
        Check(
            "OK" if config.source_path and config.source_path.exists() else "WARN",
            "配置",
            str(config.source_path) if config.source_path else "使用内置默认值",
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
            running = running_kernel()
            package_backend = distro_packages.build_backend(
                config, runner, running_release=running
            )
            checks.append(
                Check("OK", "包管理器", package_backend.describe())
            )
            try:
                update = package_backend.inspect(running)
                checks.append(
                    Check(
                        "OK",
                        "内核包",
                        "已安装 {} / 候选 {}".format(
                            update.installed_version or "无",
                            update.candidate_version,
                        ),
                    )
                )
            except distro_packages.PackageError as exc:
                checks.append(Check("WARN", "内核包", str(exc)))
        except distro_packages.PackageError as exc:
            checks.append(Check("FAIL", "包管理器", str(exc)))
    else:
        checks.append(
            Check(
                "OK" if config.patches_dir.is_dir() else "WARN",
                "补丁目录",
                str(config.patches_dir),
            )
        )
        compiler = "gcc" if runner.which("gcc") else "cc"
        checks.append(_command_check(runner, "编译器", [compiler]))
        for name, commands, optional in [
            ("make", ["make"], False),
            ("内核编译工具", ["bc", "flex", "bison", "perl"], False),
            ("归档工具", ["tar"], False),
            ("patch 工具", ["patch"], True),
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
                    "mkinitcpio 需要为这个内核创建自定义 preset",
                )
            )

        try:
            kind, source = find_kernel_config(
                config.config_source, running_kernel()
            )
            checks.append(
                Check(
                    "OK",
                    "内核配置",
                    "{} ({})".format(source or kind, kind),
                )
            )
        except SystemCheckError as exc:
            checks.append(Check("FAIL", "内核配置", str(exc)))

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
                "磁盘空间",
                "剩余 {:.1f} GiB，需要 {} GiB".format(
                    available, required
                ),
            )
        )
    except OSError as exc:
        checks.append(Check("WARN", "磁盘空间", str(exc)))

    secure = secure_boot_enabled(runner)
    if secure is True:
        if package_mode:
            checks.append(
                Check(
                    "OK",
                    "安全启动（Secure Boot）",
                    "已启用；发行版内核包通常带签名",
                )
            )
        else:
            checks.append(
                Check(
                    "WARN",
                    "安全启动（Secure Boot）",
                    "已启用；源码编译模式安装的是未签名内核",
                )
            )
    elif secure is False:
        checks.append(Check("OK", "安全启动（Secure Boot）", "已关闭"))
    else:
        checks.append(
            Check("WARN", "安全启动（Secure Boot）", "未知（mokutil 不可用）")
        )

    if package_mode and package_backend is not None:
        checks.append(
            Check(
                "OK",
                "包签名",
                "仓库元数据与软件包由 {} 校验".format(
                    config.package_manager
                    if config.package_manager != "auto"
                    else "包管理器"
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
                    "已启用 verify_pgp，但 gpg 或公钥环缺失",
                )
            )
    else:
        checks.append(
            Check(
                "WARN",
                "PGP",
                "未验证签名；仅使用 HTTPS 和 SHA-256 校验",
            )
        )

    if runner.which("systemctl"):
        enabled = runner.run(
            ["systemctl", "is-enabled", "kernel-autoupdater.timer"],
            capture=True,
            check=False,
        )
        raw_state = enabled.stdout.strip() or enabled.stderr.strip() or "未知"
        is_enabled = raw_state == "enabled"
        state = raw_state
        if (
            "Failed to connect" in raw_state
            or "System has not been booted" in raw_state
        ):
            state = "systemd 不可用"
        elif "No such file" in raw_state or "not found" in raw_state.lower():
            state = "定时器未安装"
        elif raw_state == "enabled":
            state = "已启用"
        elif raw_state == "disabled":
            state = "已停用"
        elif raw_state not in ("unknown", "未知"):
            state = "状态未知"
        checks.append(
            Check(
                "OK" if is_enabled else "WARN",
                "systemd 定时器",
                state,
            )
        )

    return checks


def print_checks(checks: List[Check]) -> None:
    labels = {"OK": "正常", "WARN": "警告", "FAIL": "失败"}
    for check in checks:
        print(
            "[{}] {} {}".format(
                labels.get(check.status, check.status),
                _pad_display(check.name, 24),
                check.detail,
            )
        )


def _pad_display(text: str, width: int) -> str:
    shown = 0
    for char in text:
        if not unicodedata.combining(char):
            shown += (
                2
                if unicodedata.east_asian_width(char) in ("W", "F")
                else 1
            )
    return text + " " * max(0, width - shown)

