#!/usr/bin/env bash
# Sign a built ScreenCap.app inside-out under a Developer ID Application identity
# (hardened runtime) so it is ready for notarization (script/notarize_app.sh).
#
# Xcode signs the .app wrapper + main executable but does NOT recurse into
# Contents/Resources/, and `xcodebuild build` typically selects an "Apple
# Development" cert, not the "Developer ID Application" cert required for
# direct distribution. This script re-signs everything explicitly with the
# Developer ID identity: every nested Mach-O first (inside-out), then the
# embedded `screencap` CLI/daemon binary, then the outer bundle last.
#
# Mirrors the proven nested-signing approach in
# macos/ScreenCap/Scripts/embed-cli.sh, but as a standalone, CI-invocable step.
#
# Usage:
#   MACOS_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#     script/sign_app.sh path/to/ScreenCap.app
#
# Environment:
#   MACOS_SIGN_IDENTITY   Required. The Developer ID Application identity, either
#                         the full common name (as above) or its SHA-1 hash.
#                         Run `security find-identity -v -p codesigning` to list.
#
# Notes:
#   - Never uses `--deep` (Apple TN2206/TN3127): it produces broken nested
#     signatures and "app is damaged" for testers. We sign each item explicitly.
#   - --timestamp requires network access (mandatory for notarization).
#   - The embedded CLI keeps its known-good hardened-runtime entitlements
#     (screencap-cli.entitlements: allow-jit + allow-unsigned-executable-memory
#     + disable-library-validation). Whether disable-library-validation can be
#     dropped once every nested dylib is Developer-ID-signed is an open question
#     deferred in the distribution plan; we use the proven set for tester builds.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ENTITLEMENTS="${REPO_ROOT}/macos/ScreenCap/ScreenCap.entitlements"
CLI_ENTITLEMENTS="${REPO_ROOT}/macos/ScreenCap/Scripts/screencap-cli.entitlements"

usage() {
  echo "usage: MACOS_SIGN_IDENTITY=\"Developer ID Application: ... (TEAMID)\" $0 <path-to-.app>" >&2
  exit 2
}

APP_PATH="${1:-}"
[ -n "${APP_PATH}" ] || usage

IDENTITY="${MACOS_SIGN_IDENTITY:-}"
if [ -z "${IDENTITY}" ] || [ "${IDENTITY}" = "-" ]; then
  echo "error: MACOS_SIGN_IDENTITY is not set (or is ad-hoc '-')." >&2
  echo "error: pass a Developer ID Application identity; list yours with:" >&2
  echo "error:   security find-identity -v -p codesigning" >&2
  exit 1
fi

if [ ! -d "${APP_PATH}" ]; then
  echo "error: ${APP_PATH} is not a directory (.app bundle expected)." >&2
  exit 1
fi

APP_NAME="$(basename "${APP_PATH%.app}")"
APP_BINARY="${APP_PATH}/Contents/MacOS/${APP_NAME}"
# SCR-196: the daemon helper is now a nested .app (bundle id com.screencap.daemon)
# in Contents/Library/LoginItems/, not the old bare Contents/Resources/screencap/.
# CLI_DIR is the helper bundle (sign walk root); CLI_BINARY its nested exec.
CLI_DIR="${APP_PATH}/Contents/Library/LoginItems/ScreencapDaemon.app"
CLI_BINARY="${CLI_DIR}/Contents/MacOS/screencap"

if [ ! -x "${APP_BINARY}" ]; then
  echo "error: main app binary not found/executable at ${APP_BINARY}." >&2
  exit 1
fi

# The embedded helper is the daemon that holds the heavy TCC grants; a build
# without it is a dev build that must not be distributed. Fail loud rather than
# producing a partial signature over a bundle missing its reason to exist.
if [ ! -x "${CLI_BINARY}" ]; then
  echo "error: embedded daemon helper not found at ${CLI_BINARY}." >&2
  echo "error: this looks like a dev build with no bundled helper. Build the" >&2
  echo "error: PyInstaller bundle (pyinstaller/screencap.spec) and rebuild the" >&2
  echo "error: app in Release before signing for distribution." >&2
  exit 1
fi

for ent in "${APP_ENTITLEMENTS}" "${CLI_ENTITLEMENTS}"; do
  if [ ! -f "${ent}" ]; then
    echo "error: entitlements file missing: ${ent}" >&2
    exit 1
  fi
done

echo "==> Signing identity: ${IDENTITY}"
echo "==> App: ${APP_PATH}"

sign() {
  # Common codesign invocation: force re-sign, hardened runtime, secure timestamp.
  codesign --force --options runtime --timestamp --sign "${IDENTITY}" "$@"
}

echo "==> Signing nested Mach-O in embedded daemon helper (inside-out)"
# Sign every nested dylib/.so first. Signing all of them with our Developer ID
# is what lets library validation pass; do this before the binaries that load them.
# NUL-delimited to survive any unusual paths inside the PyInstaller bundle.
# Batch many paths per codesign invocation (no -n 1): the PyInstaller bundle has
# hundreds of dylibs and each --timestamp is a network round-trip; batching lets
# codesign reuse the connection instead of paying ~400 separate spawns + TSA hits.
find "${CLI_DIR}" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 \
  | xargs -0 codesign --force --options runtime --timestamp --sign "${IDENTITY}"

echo "==> Signing embedded daemon helper binary (with entitlements)"
sign --entitlements "${CLI_ENTITLEMENTS}" "${CLI_BINARY}"

echo "==> Signing embedded daemon helper .app wrapper"
# The helper .app must be sealed AFTER its nested code so its Info.plist
# (CFBundleIdentifier=com.screencap.daemon) is what TCC attributes grants to.
sign "${CLI_DIR}"

# Sign any embedded frameworks/helpers if the bundle grows them later. No-op today.
if [ -d "${APP_PATH}/Contents/Frameworks" ]; then
  echo "==> Signing Contents/Frameworks"
  find "${APP_PATH}/Contents/Frameworks" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 \
    | xargs -0 -r -n 1 codesign --force --options runtime --timestamp --sign "${IDENTITY}"
  for fw in "${APP_PATH}/Contents/Frameworks/"*.framework; do
    [ -d "${fw}" ] && sign "${fw}"
  done
fi

# Sign the on-device intelligence helper (Contents/MacOS/IntelligenceHelper), a
# nested command-line tool copied in by the "Embed IntelligenceHelper" build
# phase. Xcode signs it with an Apple Development cert + get-task-allow, and
# codesign does NOT re-sign a nested Contents/MacOS/ executable when it seals the
# outer bundle — so without an explicit pass it ships the dev signature and
# notarization rejects it ("not signed with a valid Developer ID certificate",
# "signature does not include a secure timestamp", "requests the
# com.apple.security.get-task-allow entitlement"). Re-sign with hardened runtime
# + secure timestamp and no entitlements so get-task-allow is dropped. It needs
# no entitlements of its own (FoundationModels is weak-linked, no special grant).
INTELLIGENCE_HELPER="${APP_PATH}/Contents/MacOS/IntelligenceHelper"
if [ -x "${INTELLIGENCE_HELPER}" ]; then
  echo "==> Signing on-device intelligence helper (Contents/MacOS/IntelligenceHelper)"
  sign "${INTELLIGENCE_HELPER}"
fi

echo "==> Signing outer app bundle (last)"
sign --entitlements "${APP_ENTITLEMENTS}" "${APP_PATH}"

echo "==> Verifying signature (codesign --verify --deep --strict)"
codesign --verify --deep --strict --verbose=2 "${APP_PATH}"

echo "==> Confirming embedded daemon helper shows the Team identity (not adhoc)"
codesign -dvv "${CLI_BINARY}" 2>&1 | grep -E "Authority|TeamIdentifier" || true

# Hard gate (SCR-196): the helper's Designated Requirement MUST name
# com.screencap.daemon. If it doesn't (e.g. an incomplete path update left the
# nested exec ad-hoc, or the Info.plist identity is wrong), macOS will not
# attribute TCC grants to com.screencap.daemon and the whole fix is defeated —
# fail loudly here rather than shipping a silently-broken bundle.
echo "==> Asserting helper Designated Requirement names com.screencap.daemon"
if ! codesign -d -r- "${CLI_DIR}" 2>&1 | grep -q "com.screencap.daemon"; then
  echo "error: helper Designated Requirement does not name com.screencap.daemon." >&2
  codesign -d -r- "${CLI_DIR}" >&2 || true
  exit 1
fi

echo "==> Smoke-testing the signed embedded daemon helper under hardened runtime"
# Necessary but not sufficient: this is a direct exec, not the launchd/
# SMAppService daemon path. A hardened-runtime/library-validation break that
# only shows up via launchd surfaces on the real daemon launch (CI gate / tester).
if ! "${CLI_BINARY}" --version >/dev/null 2>&1; then
  echo "error: embedded daemon helper fails to launch after signing." >&2
  echo "error: check ${CLI_ENTITLEMENTS} against hardened-runtime needs." >&2
  exit 1
fi

# Gatekeeper assessment: an un-notarized Developer ID app is expected to be
# REJECTED here ("source=Unnotarized Developer ID"). Run it informationally so
# the signing step doesn't fail; the authoritative spctl check is in
# notarize_app.sh after stapling.
echo "==> Gatekeeper assessment (informational — expected to pass only after notarization)"
if spctl --assess --type exec --verbose=4 "${APP_PATH}" 2>&1; then
  echo "note: spctl already accepts the app."
else
  echo "note: spctl rejected (normal pre-notarization). Run script/notarize_app.sh next."
fi

echo "==> Done. ${APP_PATH} is Developer-ID signed and ready to notarize."
