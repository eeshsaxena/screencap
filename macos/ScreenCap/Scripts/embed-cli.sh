#!/usr/bin/env bash
# Xcode build phase: copies the PyInstaller `dist/screencap/` directory into
# Contents/Resources/screencap/ so the bundled CLIClient can resolve the binary.
#
# Tolerant of missing dist/ in dev: emits a warning so the .app still launches
# (CLIClient falls back to SCREENCAP_CLI_PATH or SCREENCAP_DEV_REPO_ROOT).

set -euo pipefail

# Resolve repo root: macos/ is one level below the repo root.
REPO_ROOT="${SRCROOT}/.."
SOURCE_DIR="${SCREENCAP_CLI_DIST_DIR:-${REPO_ROOT}/dist/screencap}"
DEST_DIR="${BUILT_PRODUCTS_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/screencap"
LAUNCHER="${BUILT_PRODUCTS_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/screencap-daemon-launcher"

write_daemon_launcher() {
    mkdir -p "$(dirname "${LAUNCHER}")"
    cat >"${LAUNCHER}" <<'EOF'
#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ "${SCREENCAP_DAEMON_USE_DEV_SOURCE:-0}" = "1" ] && [ -n "${SCREENCAP_DEV_REPO_ROOT:-}" ] && [ -d "${SCREENCAP_DEV_REPO_ROOT}/src/screencap" ]; then
    PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
    export PATH

    if [ -n "${PYTHONPATH:-}" ]; then
        export PYTHONPATH="${SCREENCAP_DEV_REPO_ROOT}/src:${PYTHONPATH}"
    else
        export PYTHONPATH="${SCREENCAP_DEV_REPO_ROOT}/src"
    fi

    if [ -n "${SCREENCAP_DEV_PYTHON:-}" ] && [ -x "${SCREENCAP_DEV_PYTHON}" ]; then
        exec "${SCREENCAP_DEV_PYTHON}" -m screencap "$@"
    fi

    PYTHON="$(command -v python3)"
    PYTHON="$("${PYTHON}" -c 'import sys; print(sys.executable)')"
    exec "${PYTHON}" -m screencap "$@"
fi

exec "${SCRIPT_DIR}/screencap/screencap" "$@"
EOF
    chmod 0755 "${LAUNCHER}"
}

write_daemon_launcher

if [ ! -d "${SOURCE_DIR}" ]; then
    echo "warning: dist/screencap/ not found at ${SOURCE_DIR}"
    echo "warning: ScreenCap.app will NOT have a bundled CLI."
    echo "warning: Set SCREENCAP_CLI_PATH or run \`pyinstaller pyinstaller/screencap.spec\` from the repo root."
    exit 0
fi

if [ ! -x "${SOURCE_DIR}/screencap" ]; then
    echo "warning: ${SOURCE_DIR}/screencap is not executable. Skipping embed."
    exit 0
fi

mkdir -p "$(dirname "${DEST_DIR}")"
rm -rf "${DEST_DIR}"
ditto "${SOURCE_DIR}" "${DEST_DIR}"

echo "Embedded screencap CLI from ${SOURCE_DIR} -> ${DEST_DIR}"
