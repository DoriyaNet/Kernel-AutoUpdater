#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "install.sh must run as root" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
ENABLE_TIMER="${ENABLE_TIMER:-1}"

if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "python3 is required" >&2
    exit 1
fi

"${PYTHON}" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9 or newer is required")
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
    echo "installed /etc/kernel-autoupdater/config.ini"
else
    echo "kept existing /etc/kernel-autoupdater/config.ini"
fi

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
    if [[ "${ENABLE_TIMER}" == "1" ]]; then
        systemctl enable --now kernel-autoupdater.timer
        echo "enabled kernel-autoupdater.timer (daily check)"
    else
        echo "timer installed but not enabled (ENABLE_TIMER=0)"
    fi
else
    echo "systemd not running; timer was installed but not enabled" >&2
fi

echo
echo "Installed. Next steps:"
echo "  1. edit /etc/kernel-autoupdater/config.ini"
echo "  2. run: kau doctor"
echo "  3. run: kau check"
echo "  4. preview: kau update --dry-run"
echo
echo "Automatic installs stay disabled until auto_update = true."

