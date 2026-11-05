#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${KAU_REPO_URL:-https://github.com/DoriyaNet/Kernel-AutoUpdater}"
REF="${KAU_REF:-master}"
ENABLE_TIMER="${ENABLE_TIMER:-1}"
REF_GIVEN=0
LOCAL_ROOT=""
TMPDIR_INSTALL=""
SCRIPT_PATH="${BASH_SOURCE[0]:-}"

usage() {
    cat <<'USAGE'
One-click installer for Kernel AutoUpdater (kau).

Usage:
  curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash
  curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash -s -- --ref <branch-or-tag>
  sudo bash install.sh

Options:
  --ref <branch-or-tag>  Install from a branch or tag instead of master
  --no-timer             Install the systemd timer but do not enable it
  -h, --help             Show this help

Environment:
  KAU_REF                Default ref to install (default: master)
  KAU_REPO_URL           GitHub repository URL (for forks/testing)
  ENABLE_TIMER           Set to 0 to leave the systemd timer disabled
  PYTHON                 Python interpreter to use (default: python3)
USAGE
}

die() {
    echo "install.sh: $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${TMPDIR_INSTALL}" && -d "${TMPDIR_INSTALL}" ]]; then
        local resolved=""
        resolved="$(cd "${TMPDIR_INSTALL}" 2>/dev/null && pwd -P)" || return 0
        case "${resolved}" in
            /tmp/kau-install.*)
                rm -rf -- "${resolved}"
                ;;
            *)
                echo "install.sh: refusing to remove unexpected path: ${resolved}" >&2
                ;;
        esac
    fi
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ref)
                [[ $# -ge 2 && -n "$2" ]] || die "--ref requires a branch or tag name"
                REF="$2"
                REF_GIVEN=1
                shift 2
                ;;
            --ref=*)
                REF="${1#--ref=}"
                [[ -n "${REF}" ]] || die "--ref requires a branch or tag name"
                REF_GIVEN=1
                shift
                ;;
            --no-timer)
                ENABLE_TIMER=0
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "unknown argument: $1 (use --help for usage)"
                ;;
        esac
    done
}

check_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        die "must run as root (try: sudo bash install.sh)"
    fi
}

check_deps() {
    local missing=0
    local cmd
    local python_bin="${PYTHON:-python3}"

    for cmd in bash tar "${python_bin}"; do
        if ! command -v "${cmd}" >/dev/null 2>&1; then
            echo "install.sh: required command not found: ${cmd}" >&2
            missing=1
        fi
    done

    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        echo "install.sh: required command not found: curl or wget" >&2
        missing=1
    fi

    [[ "${missing}" -eq 0 ]] || exit 1
}

validate_ref() {
    [[ -n "${REF}" ]] || die "ref must not be empty"
    [[ "${REF}" != -* ]] || die "invalid ref: ${REF}"
    [[ "${REF}" != *..* ]] || die "invalid ref: ${REF}"
    [[ "${REF}" != /* && "${REF}" != */ ]] || die "invalid ref: ${REF}"
    if [[ ! "${REF}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
        die "invalid ref: ${REF} (use a branch or tag name)"
    fi
}

download() {
    local url="$1"
    local dest="$2"

    if command -v curl >/dev/null 2>&1; then
        if curl -fsSL --retry 2 --connect-timeout 15 -o "${dest}" "${url}"; then
            return 0
        fi
        echo "install.sh: curl failed, trying wget..." >&2
    fi

    if command -v wget >/dev/null 2>&1; then
        if wget -q --timeout=15 --tries=2 -O "${dest}" "${url}"; then
            return 0
        fi
    fi

    return 1
}

find_local_checkout() {
    local script_path="${SCRIPT_PATH}"

    if [[ -n "${script_path}" && -f "${script_path}" ]]; then
        local script_dir=""
        script_dir="$(cd "$(dirname "${script_path}")" && pwd -P)"
        if [[ -f "${script_dir}/src/kernel_autoupdater/__init__.py" ]]; then
            LOCAL_ROOT="${script_dir}"
        fi
    fi
}

run_local() {
    [[ -f "${LOCAL_ROOT}/scripts/install.sh" ]] \
        || die "local checkout is missing scripts/install.sh"

    echo "Using local checkout: ${LOCAL_ROOT}"
    if [[ "${REF_GIVEN}" -eq 1 ]]; then
        echo "Note: --ref is ignored for a local checkout."
    fi

    ENABLE_TIMER="${ENABLE_TIMER}" bash "${LOCAL_ROOT}/scripts/install.sh"
}

run_download() {
    validate_ref
    echo "Downloading Kernel AutoUpdater ref '${REF}'..."

    TMPDIR_INSTALL="$(mktemp -d /tmp/kau-install.XXXXXX)" \
        || die "could not create a temporary directory under /tmp"

    local archive="${TMPDIR_INSTALL}/source.tar.gz"
    local extract_dir="${TMPDIR_INSTALL}/checkout"
    local url=""
    local downloaded=0
    local kind

    for kind in heads tags; do
        url="${REPO_URL}/archive/refs/${kind}/${REF}.tar.gz"
        rm -f "${archive}"
        echo "  ${url}"
        if download "${url}" "${archive}"; then
            downloaded=1
            break
        fi
        echo "install.sh: download failed: ${url}" >&2
    done

    [[ "${downloaded}" -eq 1 ]] \
        || die "could not download '${REF}' as a branch or tag"
    [[ -s "${archive}" ]] || die "downloaded archive is empty"

    mkdir -p "${extract_dir}"
    tar -xzf "${archive}" -C "${extract_dir}" \
        || die "failed to extract the downloaded archive"

    local installer=""
    installer="$(find "${extract_dir}" -maxdepth 3 -type f \
        -path '*/scripts/install.sh' -print -quit)"
    [[ -n "${installer}" ]] \
        || die "downloaded archive does not contain scripts/install.sh"

    local source_root=""
    source_root="$(cd "$(dirname "${installer}")/.." && pwd -P)"
    [[ -f "${source_root}/src/kernel_autoupdater/__init__.py" ]] \
        || die "downloaded archive does not look like a Kernel AutoUpdater checkout"

    ENABLE_TIMER="${ENABLE_TIMER}" bash "${source_root}/scripts/install.sh"
}

main() {
    parse_args "$@"
    check_root
    check_deps
    trap cleanup EXIT

    find_local_checkout
    if [[ -n "${LOCAL_ROOT}" ]]; then
        run_local
    else
        run_download
    fi
}

main "$@"
