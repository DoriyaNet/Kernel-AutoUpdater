#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "install.sh 需要 root 权限，请使用 sudo 运行" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
ENABLE_TIMER="${ENABLE_TIMER:-1}"

if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "缺少 python3" >&2
    exit 1
fi

"${PYTHON}" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("需要 Python 3.9 或更高版本")
PY

TMPDIR_BUILD="$(mktemp -d)"
cleanup() {
    rm -rf "${TMPDIR_BUILD}"
}
trap cleanup EXIT

mkdir -p "${TMPDIR_BUILD}/app"
cp -a "${ROOT}/src/kernel_autoupdater" "${TMPDIR_BUILD}/app/"
find "${TMPDIR_BUILD}/app" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

cat > "${TMPDIR_BUILD}/app/__main__.py" <<'PY'
from kernel_autoupdater.cli import main

raise SystemExit(main())
PY

"${PYTHON}" -m zipapp "${TMPDIR_BUILD}/app" \
    -p "/usr/bin/env python3" \
    -o "${TMPDIR_BUILD}/kau"

install -D -m 0755 "${TMPDIR_BUILD}/kau" /usr/local/bin/kau
ln -sfn /usr/local/bin/kau /usr/local/bin/kmenu
install -D -m 0644 "${ROOT}/packaging/systemd/kernel-autoupdater.service" \
    /etc/systemd/system/kernel-autoupdater.service
install -D -m 0644 "${ROOT}/packaging/systemd/kernel-autoupdater.timer" \
    /etc/systemd/system/kernel-autoupdater.timer
install -D -m 0644 "${ROOT}/packaging/kernel-autoupdater.logrotate" \
    /etc/logrotate.d/kernel-autoupdater
install -D -m 0644 "${ROOT}/README.md" \
    /usr/local/share/doc/kernel-autoupdater/README.md

install -d -m 0755 /etc/kernel-autoupdater
install -d -m 0700 /etc/kernel-autoupdater/patches
install -d -m 0755 /var/lib/kernel-autoupdater

if [[ ! -f /etc/kernel-autoupdater/config.ini ]]; then
    install -m 0644 "${ROOT}/packaging/config.ini" \
        /etc/kernel-autoupdater/config.ini
    echo "已安装 /etc/kernel-autoupdater/config.ini"
else
    echo "已保留现有 /etc/kernel-autoupdater/config.ini"
fi

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
    if [[ "${ENABLE_TIMER}" == "1" ]]; then
        systemctl enable --now kernel-autoupdater.timer
        echo "已启用 kernel-autoupdater.timer（每日检查）"
    else
        echo "定时器已安装但未启用（ENABLE_TIMER=0）"
    fi
else
    echo "systemd 未运行；定时器已安装但未启用" >&2
fi

echo
echo "安装完成。kmenu 是 kau 的快捷命令。"
echo "下一步："
echo "  1. 运行 kau（或 kmenu）打开交互菜单"
echo "  2. 运行 kau doctor 做环境自检"
echo "  3. 运行 kau check 检查更新"
echo "  4. 运行 kau update --dry-run 预览更新"
echo "  5. 编辑 /etc/kernel-autoupdater/config.ini"
echo "  6. 之后可运行 kau self-update --check"
echo
echo "在 auto_update = true 之前不会自动安装内核。"

