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
