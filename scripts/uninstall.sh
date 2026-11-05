#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "uninstall.sh must run as root" >&2
    exit 1
fi

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl disable --now kernel-autoupdater.timer 2>/dev/null || true
fi

rm -f /etc/systemd/system/kernel-autoupdater.service
rm -f /etc/systemd/system/kernel-autoupdater.timer
rm -f /etc/logrotate.d/kernel-autoupdater
rm -f /usr/local/bin/kau
rm -rf /usr/local/share/doc/kernel-autoupdater

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
fi

echo "Removed the kau binary, systemd units and logrotate rule."
echo "Configuration and built kernels were preserved:"
echo "  /etc/kernel-autoupdater"
echo "  /var/lib/kernel-autoupdater"
echo
echo "Remove them manually only after verifying you no longer need them."

