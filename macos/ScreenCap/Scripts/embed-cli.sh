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
    # The dev-source branch lets the LaunchAgent exec the in-repo `screencap`
    # module directly, bypassing the PyInstaller bundle for fast iteration.
    # We only emit it for Debug builds — release builds get a launcher that
    # only execs the bundled binary, so SCREENCAP_DAEMON_USE_DEV_SOURCE is
    # an inert env var in shipped clients.
    if [ "${CONFIGURATION:-}" = "Debug" ]; then
        cat >"${LAUNCHER}" <<'EOF'
#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# Debug-only stderr capture: the LaunchAgent plist has no StandardErrorPath,
# so without this redirect daemon stderr (engine events forwarded by
# _stderr_pump, supervisor logger output, fatal tracebacks) goes to
# /dev/null and a daemon-side bug is effectively unobservable. The file
# is append-only and grows unbounded — Release launchers do NOT do this.
# Mirrors `auto-serve.log` for the CLI auto-spawn path. SCR-69.
SCREENCAP_LOG_DIR="${HOME}/.screencap/run"
umask 077
mkdir -p "${SCREENCAP_LOG_DIR}" 2>/dev/null || true
if [ -d "${SCREENCAP_LOG_DIR}" ]; then
    exec 2>>"${SCREENCAP_LOG_DIR}/serve.log"
fi

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

    # `command -v` returning empty under `set -eu` would abort the script and
    # cause launchd to immediately respawn us, creating a tight restart loop.
    # Fall through to the bundled binary path instead when python3 is absent
    # or sys.executable resolution fails.
    PYTHON="$(command -v python3 || true)"
    if [ -n "${PYTHON}" ]; then
        RESOLVED="$("${PYTHON}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
        if [ -n "${RESOLVED}" ] && [ -x "${RESOLVED}" ]; then
            exec "${RESOLVED}" -m screencap "$@"
        fi
    fi
    # Dev-source path requested but unusable — fall through to bundled exec.
fi

exec "${SCRIPT_DIR}/screencap/screencap" "$@"
EOF
    else
        cat >"${LAUNCHER}" <<'EOF'
#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

exec "${SCRIPT_DIR}/screencap/screencap" "$@"
EOF
    fi
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
