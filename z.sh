#!/usr/bin/env bash
# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

# Thin wrapper that delegates to the nanvix-zutil CLI.
# Self-bootstraps nanvix-zutil into .nanvix/venv/ if it is not already installed.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
VENV="$REPO_ROOT/.nanvix/venv"
VERSION_FILE="$REPO_ROOT/.zutils-version"

# Resolve the pinned nanvix-zutil version. `.zutils-version` at the repo
# root is the sole source of truth: no env-var override, no GitHub fetch.
# Downstream consumers commit this file; CI and developers see the same
# pin without ambient configuration.
function _resolve_zutil_version() {
    # RAW_ZUTIL_VERSION and ZUTIL_VERSION are intentionally global;
    # they are consumed by bootstrap() below.
    if [ ! -f "$VERSION_FILE" ]; then
        echo "Error: $VERSION_FILE not found." >&2
        echo "       Create it with a pinned nanvix-zutil version, e.g. 'v0.10.2'." >&2
        exit 1
    fi
    local raw
    raw="$(tr -d '[:space:]' <"$VERSION_FILE")"
    if [ -z "$raw" ]; then
        echo "Error: $VERSION_FILE is empty." >&2
        exit 1
    fi
    if [[ ! "$raw" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+([.-][[:alnum:].-]+)?$ ]]; then
        echo "Error: invalid nanvix-zutil version '$raw' in $VERSION_FILE (expected vX.Y.Z)." >&2
        exit 1
    fi
    RAW_ZUTIL_VERSION="$raw"
    ZUTIL_VERSION="${RAW_ZUTIL_VERSION#v}"
}
_resolve_zutil_version

# Resolve venv layout (bin/ vs Scripts/) based on what exists on disk.
# Can be called before venv creation to initialize default paths; call it
# again after venv creation to pick up the actual layout.
function _resolve_venv_paths() {
    if [ -d "$VENV/Scripts" ]; then
        VENV_BIN="$VENV/Scripts/nanvix-zutil.exe"
        VENV_PYTHON="$VENV/Scripts/python.exe"
    else
        VENV_BIN="$VENV/bin/nanvix-zutil"
        VENV_PYTHON="$VENV/bin/python"
    fi
}
_resolve_venv_paths
ZUTIL_GLOBAL_VERSION="$(nanvix-zutil --version 2>/dev/null || true)"

# Extract --with-zutils PATH and --with-nanvix PATH before forwarding to
# nanvix-zutil.  The nanvix-zutil CLI inspects positional args to find the
# subcommand; either flag's PATH argument would be mistaken for a subcommand.
#
# --with-zutils PATH (optional): install nanvix-zutil from a local source
# tree (editable) instead of fetching the pinned wheel from GitHub Releases.
# Useful when iterating on zutils itself against a downstream consumer repo.
#
#   ./z.sh --with-zutils ~/src/zutils build
#   ./z.sh --with-zutils=~/src/zutils build
#
# The path must point at a nanvix-zutil source checkout (a directory
# containing pyproject.toml).  The venv version-match check is bypassed; the
# editable install is rebuilt only when the recorded source path changes.
#
# --with-nanvix PATH is forwarded to z.py via the WITH_NANVIX env var.
WITH_ZUTILS=""
_resolve_zutils_path() {
    local raw="$1"
    if [ ! -e "$raw" ]; then
        echo "ERROR: --with-zutils path does not exist: ${raw}" >&2
        exit 1
    fi
    if [ ! -d "$raw" ]; then
        echo "ERROR: --with-zutils is not a directory: ${raw}" >&2
        exit 1
    fi
    if ! WITH_ZUTILS="$(cd -- "$raw" 2>/dev/null && pwd -P)"; then
        echo "ERROR: --with-zutils is not accessible (cd failed): ${raw}" >&2
        exit 1
    fi
    if [ ! -f "$WITH_ZUTILS/pyproject.toml" ]; then
        echo "ERROR: --with-zutils is not a Python source tree (no pyproject.toml): ${WITH_ZUTILS}" >&2
        exit 1
    fi
}

_resolve_nanvix_path() {
    local raw="$1"
    if ! WITH_NANVIX="$(cd -- "$raw" 2>/dev/null && pwd -P)"; then
        echo "ERROR: --with-nanvix path does not exist or is not a directory: $raw" >&2
        exit 1
    fi
    export WITH_NANVIX
}

ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-zutils=*)
            _resolve_zutils_path "${1#--with-zutils=}"
            shift
            ;;
        --with-zutils)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --with-zutils requires a path argument" >&2
                exit 1
            fi
            _resolve_zutils_path "$2"
            shift 2
            ;;
        --with-nanvix=*)
            _resolve_nanvix_path "${1#--with-nanvix=}"
            shift
            ;;
        --with-nanvix)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --with-nanvix requires a path argument" >&2
                exit 1
            fi
            _resolve_nanvix_path "$2"
            shift 2
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

function bootstrap_local() {
    # Install nanvix-zutil from $WITH_ZUTILS as an editable install.
    echo "nanvix-zutil -- installing editable from ${WITH_ZUTILS}..." >&2
    if ! command -v python3 &>/dev/null; then
        echo "Error: python3 not found. Install Python 3 and ensure python3 is on PATH." >&2
        exit 1
    fi
    if [ -d "$VENV" ]; then
        python3 -m venv --clear "$VENV"
    else
        python3 -m venv "$VENV"
    fi
    _resolve_venv_paths
    "$VENV_PYTHON" -m pip install --quiet -e "${WITH_ZUTILS}[lint]"
    printf '%s\n' "$WITH_ZUTILS" >"$VENV/.with-zutils"
}

function bootstrap() {
    # nanvix-zutil version comes from .zutils-version (resolved above).
    local reason="${1:-not found}"
    echo "nanvix-zutil ${reason} -- bootstrapping nanvix-zutil==${ZUTIL_VERSION}..." >&2

    if ! command -v python3 &>/dev/null; then
        echo "Error: python3 not found. Install Python 3 and ensure python3 is on PATH." >&2
        exit 1
    fi

    WHEEL_URL="https://github.com/nanvix/zutils/releases/download/v${ZUTIL_VERSION}/nanvix_zutil-${ZUTIL_VERSION}-py3-none-any.whl"
    if [ -d "$VENV" ]; then
        python3 -m venv --clear "$VENV"
    else
        python3 -m venv "$VENV"
    fi
    # Re-resolve paths now that the venv exists (Scripts/ vs bin/).
    _resolve_venv_paths
    "$VENV_PYTHON" -m pip install --quiet "nanvix-zutil[lint] @ ${WHEEL_URL}"
}

# Prefer the venv copy if it exists; otherwise use the global install.
BIN=""
if [ -n "$WITH_ZUTILS" ]; then
    RECORDED_ZUTILS=""
    if [ -f "$VENV/.with-zutils" ]; then
        RECORDED_ZUTILS="$(cat "$VENV/.with-zutils")"
    fi
    if [ ! -x "$VENV_BIN" ] || [ "$RECORDED_ZUTILS" != "$WITH_ZUTILS" ]; then
        bootstrap_local
    fi
    BIN="$VENV_BIN"
elif [ ! -d "$VENV" ] && [ -z "$ZUTIL_GLOBAL_VERSION" ]; then
    bootstrap
    BIN="$VENV_BIN"
elif [ -x "$VENV_BIN" ]; then
    VENV_VERSION="$("$VENV_BIN" --version 2>/dev/null || true)"
    if [ "$VENV_VERSION" != "nanvix-zutil ${ZUTIL_VERSION}" ]; then
        echo "Warning: venv nanvix-zutil version mismatch. Expected ${ZUTIL_VERSION}, found ${VENV_VERSION}. Re-bootstrapping..." >&2
        bootstrap
    fi
    BIN="$VENV_BIN"
elif [ -d "$VENV" ] && ! command -v nanvix-zutil &>/dev/null; then
    echo "Warning: incomplete venv detected (binary missing). Re-running bootstrap..." >&2
    bootstrap
    BIN="$VENV_BIN"
else
    BIN="nanvix-zutil"
    if [ "$ZUTIL_GLOBAL_VERSION" != "nanvix-zutil ${ZUTIL_VERSION}" ]; then
        echo "Warning: nanvix-zutil global install does not match expected version. Expected ${ZUTIL_VERSION}, found ${ZUTIL_GLOBAL_VERSION}." >&2
        bootstrap "version mismatch"
        BIN="$VENV_BIN"
    fi
fi

# On Windows (Git Bash / MSYS2) the venv's python.exe is locked while it runs,
# so the Python distclean command cannot delete it.  Run without exec so the
# shell can remove the venv after the interpreter exits.
if [[ "${ARGS[0]:-}" == "distclean" ]]; then
    "$BIN" "${ARGS[@]}"
    EC=$?
    if [ -d "$VENV" ]; then
        rm -rf "$VENV" 2>/dev/null || true
    fi
    exit $EC
fi

exec "$BIN" "${ARGS[@]}"
