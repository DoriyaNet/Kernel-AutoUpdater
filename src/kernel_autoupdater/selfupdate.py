"""Update the installed Kernel AutoUpdater program from GitHub."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

from kernel_autoupdater import __version__
from kernel_autoupdater.kernelorg import USER_AGENT, DownloadError, download

LOGGER = logging.getLogger(__name__)

REPO = "DoriyaNet/Kernel-AutoUpdater"
DEFAULT_REF = "master"
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_REMOTE_VERSION_RE = re.compile(
    r'^version\s*=\s*"([^"]+)"', re.MULTILINE
)


class SelfUpdateError(RuntimeError):
    """Raised when the program cannot update itself."""


def _version_key(value: str) -> tuple:
    parts = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(parts) if parts else (0,)


def _validate_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if (
        not ref
        or not _REF_RE.match(ref)
        or ".." in ref
        or ref.startswith("-")
        or ref.startswith("/")
        or ref.endswith("/")
    ):
        raise SelfUpdateError("无效的更新 ref: {!r}".format(ref))
    return ref


def fetch_remote_version(ref: str = DEFAULT_REF, timeout: int = 30) -> str:
    """Return the version declared by pyproject.toml on the remote ref."""

    ref = _validate_ref(ref)
    url = "https://raw.githubusercontent.com/{}/{}/pyproject.toml".format(
        REPO, quote(ref, safe="/")
    )
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset)
    except Exception as exc:
        raise SelfUpdateError(
            "无法检查程序版本 ({}): {}".format(url, exc)
        ) from exc

    match = _REMOTE_VERSION_RE.search(text)
    if not match:
        raise SelfUpdateError(
            "远程 pyproject.toml 中没有找到 version 字段: {}".format(url)
        )
    return match.group(1)


def _timer_enabled() -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        result = subprocess.run(
            [systemctl, "is-enabled", "kernel-autoupdater.timer"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip().startswith("enabled")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with tarfile.open(str(archive), "r:*") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if base not in target.parents and target != base:
                raise SelfUpdateError(
                    "拒绝解压到临时目录之外的路径: {}".format(member.name)
                )
            if member.issym() or member.islnk():
                link_base = target.parent if member.issym() else base
                link_target = (link_base / member.linkname).resolve()
                if base not in link_target.parents and link_target != base:
                    raise SelfUpdateError(
                        "拒绝解压不安全的链接: {}".format(member.name)
                    )
        if hasattr(tarfile, "data_filter"):
            tar.extractall(str(destination), filter="data")
        else:  # pragma: no cover - Python < 3.12
            tar.extractall(str(destination))


def _find_source_root(extract_dir: Path) -> Path:
    candidates: List[Path] = []
    for child in extract_dir.iterdir():
        if child.is_dir():
            candidates.append(child)
    for candidate in candidates:
        installer = candidate / "scripts" / "install.sh"
        package = candidate / "src" / "kernel_autoupdater" / "__init__.py"
        if installer.is_file() and package.is_file():
            return candidate
    raise SelfUpdateError("下载的源码包中找不到 scripts/install.sh")


def _download_archive(ref: str, destination: Path, timeout: int) -> None:
    errors = []
    for kind in ("heads", "tags"):
        url = "https://github.com/{}/archive/refs/{}/{}.tar.gz".format(
            REPO, kind, quote(ref, safe="/")
        )
        try:
            LOGGER.info("正在下载程序更新: %s", url)
            download(url, destination, timeout=timeout)
            return
        except DownloadError as exc:
            errors.append("{}: {}".format(kind, exc))
    raise SelfUpdateError(
        "无法下载程序更新（master/tag 均失败）: {}".format("; ".join(errors))
    )


def run_self_update(
    ref: str = DEFAULT_REF,
    check_only: bool = False,
    force: bool = False,
    assume_yes: bool = False,
) -> int:
    """Check or install the latest program build from GitHub.

    ``check_only`` returns 10 when a newer version exists, 0 when current.
    Actual updates return 0 on success.
    """

    ref = _validate_ref(ref)
    current = __version__
    remote: Optional[str] = None
    try:
        remote = fetch_remote_version(ref)
    except SelfUpdateError as exc:
        if check_only:
            raise
        LOGGER.warning("%s；将直接重新安装 %s", exc, ref)

    if remote is not None:
        LOGGER.info("当前程序版本: %s", current)
        LOGGER.info("远程程序版本: %s (%s)", remote, ref)
        newer = _version_key(remote) > _version_key(current)
        if check_only:
            if newer:
                print("发现新版本: {} -> {}".format(current, remote))
                return 10
            print("无需更新：本地 {}，远程 {}".format(current, remote))
            return 0
        if not newer and not force:
            print("无需更新：本地 {}，远程 {}".format(current, remote))
            return 0

    if not sys.platform.startswith("linux"):
        raise SelfUpdateError("程序自更新仅支持 Linux")
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        raise SelfUpdateError("程序自更新需要 root，请使用 sudo 运行")
    if not shutil.which("bash"):
        raise SelfUpdateError("程序自更新需要 bash")

    if not assume_yes:
        print("将从 GitHub 重新安装 kau（ref: {}）。".format(ref))
        if not sys.stdin.isatty():
            raise SelfUpdateError("需要确认；请重新运行并加 --yes")
        answer = input("继续更新程序？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消。")
            return 0

    with tempfile.TemporaryDirectory(prefix="kau-self-update-") as temp:
        temp_dir = Path(temp)
        archive = temp_dir / "source.tar.gz"
        _download_archive(ref, archive, timeout=120)
        extract_dir = temp_dir / "checkout"
        _safe_extract(archive, extract_dir)
        source_root = _find_source_root(extract_dir)
        installer = source_root / "scripts" / "install.sh"
        if not installer.is_file():
            raise SelfUpdateError("源码包中没有 scripts/install.sh")

        env = os.environ.copy()
        env["ENABLE_TIMER"] = "1" if _timer_enabled() else "0"
        env.setdefault(
            "PYTHON",
            sys.executable or "python3",
        )
        LOGGER.info("运行安装脚本以更新 kau ...")
        result = subprocess.run(
            ["bash", str(installer)],
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise SelfUpdateError(
                "程序安装脚本失败，退出码 {}".format(result.returncode)
            )

    print("程序已更新（{}）。请重新运行 kau 以使用新版本。".format(ref))
    return 0

