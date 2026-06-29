#!/usr/bin/env bash
# Notarize a Developer-ID-signed ScreenCap.app, staple it, package it into a
# signed + notarized + stapled DMG, and emit a checksum. The result opens with
# no Gatekeeper friction, even offline.
#
# Run script/sign_app.sh FIRST. This script assumes the .app is already signed
# inside-out with a Developer ID Application identity and hardened runtime.
#
# A DMG (not a raw zip) is the distribution format: drag-to-Applications strips
# quarantine and avoids App Translocation, which would otherwise relocate the
# bundle and break the app's by-path resolution of its embedded CLI.
#
# Usage:
#   APPLE_NOTARY_KEY_P8=/path/to/AuthKey_XXXX.p8 \
#   APPLE_NOTARY_KEY_ID=XXXXXXXXXX \
#   APPLE_NOTARY_ISSUER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
#   MACOS_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#     script/notarize_app.sh path/to/ScreenCap.app [version]
#
# Environment:
#   APPLE_NOTARY_KEY_P8          Path to the App Store Connect API key (.p8).
#   APPLE_NOTARY_KEY_P8_BASE64   Alternative to the path: base64 of the .p8
#                                contents (for CI secrets). Decoded to a temp
#                                file with 0600 perms and removed on exit.
#   APPLE_NOTARY_KEY_ID          The key's Key ID.
#   APPLE_NOTARY_ISSUER_ID       The App Store Connect issuer UUID.
#   MACOS_SIGN_IDENTITY          Developer ID Application identity (to sign the DMG).
#   OUTPUT_DIR                   Where the DMG lands (default: .build/dmg).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "usage: $0 <path-to-.app> [version]   (see header for required env)" >&2
  exit 2
}

APP_PATH="${1:-}"
[ -n "${APP_PATH}" ] || usage
[ -d "${APP_PATH}" ] || { echo "error: ${APP_PATH} is not a .app bundle." >&2; exit 1; }
# Resolve to absolute: `defaults read` requires an absolute plist path.
APP_PATH="$(cd "$(dirname "${APP_PATH}")" && pwd)/$(basename "${APP_PATH}")"

APP_NAME="$(basename "${APP_PATH%.app}")"
INFO_PLIST="${APP_PATH}/Contents/Info.plist"

# Version: explicit arg wins, else read CFBundleShortVersionString from the app.
VERSION="${2:-}"
if [ -z "${VERSION}" ] && [ -f "${INFO_PLIST}" ]; then
  VERSION="$(/usr/bin/defaults read "${INFO_PLIST}" CFBundleShortVersionString 2>/dev/null || true)"
fi
VERSION="${VERSION:-0.0.0}"

IDENTITY="${MACOS_SIGN_IDENTITY:-}"
[ -n "${IDENTITY}" ] && [ "${IDENTITY}" != "-" ] || {
  echo "error: MACOS_SIGN_IDENTITY must be a Developer ID Application identity (to sign the DMG)." >&2
  exit 1
}

KEY_ID="${APPLE_NOTARY_KEY_ID:-}"
ISSUER_ID="${APPLE_NOTARY_ISSUER_ID:-}"
[ -n "${KEY_ID}" ] || { echo "error: APPLE_NOTARY_KEY_ID is required." >&2; exit 1; }
[ -n "${ISSUER_ID}" ] || { echo "error: APPLE_NOTARY_ISSUER_ID is required." >&2; exit 1; }

# Resolve the .p8 key: explicit path, or decode the base64 form to a temp file.
TMP_KEY=""
cleanup() { [ -n "${TMP_KEY}" ] && rm -f "${TMP_KEY}" || true; }
trap cleanup EXIT

KEY_PATH="${APPLE_NOTARY_KEY_P8:-}"
if [ -z "${KEY_PATH}" ] && [ -n "${APPLE_NOTARY_KEY_P8_BASE64:-}" ]; then
  TMP_KEY="$(mktemp -t screencap-notary-key)"
  chmod 600 "${TMP_KEY}"
  printf '%s' "${APPLE_NOTARY_KEY_P8_BASE64}" | base64 --decode > "${TMP_KEY}"
  KEY_PATH="${TMP_KEY}"
fi
[ -n "${KEY_PATH}" ] && [ -f "${KEY_PATH}" ] || {
  echo "error: provide APPLE_NOTARY_KEY_P8 (path) or APPLE_NOTARY_KEY_P8_BASE64." >&2
  exit 1
}

OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.build/dmg}"
mkdir -p "${OUTPUT_DIR}"
ZIP_PATH="${OUTPUT_DIR}/${APP_NAME}.zip"
DMG_PATH="${OUTPUT_DIR}/${APP_NAME}-${VERSION}.dmg"
STAGING_DIR="${OUTPUT_DIR}/dmg-staging"

NOTARY_AUTH=(--key "${KEY_PATH}" --key-id "${KEY_ID}" --issuer "${ISSUER_ID}")

# notarytool wants a container; ditto -c -k --keepParent makes the archive Apple
# expects (a plain `zip` mangles symlinks/metadata and fails notarization).
notarize() {
  local path="$1"
  local archive submit_out submission_id status
  case "${path}" in
    *.app)
      archive="${ZIP_PATH}"
      rm -f "${archive}"
      /usr/bin/ditto -c -k --keepParent "${path}" "${archive}"
      ;;
    *)
      archive="${path}"  # DMG submits directly
      ;;
  esac

  echo "==> Submitting $(basename "${path}") to notarytool (this can take minutes)..."
  set +e
  submit_out="$(xcrun notarytool submit "${archive}" "${NOTARY_AUTH[@]}" --wait --output-format json 2>&1)"
  local rc=$?
  set -e
  echo "${submit_out}"

  submission_id="$(printf '%s' "${submit_out}" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p' | head -n 1)"
  status="$(printf '%s' "${submit_out}" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p' | tail -n 1)"

  if [ "${status}" != "Accepted" ] || [ "${rc}" -ne 0 ]; then
    echo "error: notarization for $(basename "${path}") was not Accepted (status=${status:-unknown})." >&2
    if [ -n "${submission_id}" ]; then
      echo "==> Fetching notarytool log for ${submission_id}:" >&2
      xcrun notarytool log "${submission_id}" "${NOTARY_AUTH[@]}" >&2 || true
    fi
    exit 1
  fi
}

# ---- 1. Notarize + staple the app -------------------------------------------
notarize "${APP_PATH}"
echo "==> Stapling the app"
xcrun stapler staple "${APP_PATH}"
xcrun stapler validate "${APP_PATH}"

# ---- 2. Build the DMG -------------------------------------------------------
echo "==> Building DMG"
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"
/usr/bin/ditto "${APP_PATH}" "${STAGING_DIR}/${APP_NAME}.app"
ln -s /Applications "${STAGING_DIR}/Applications"

rm -f "${DMG_PATH}"
hdiutil create \
  -volname "${APP_NAME}" \
  -srcfolder "${STAGING_DIR}" \
  -ov -format UDZO \
  "${DMG_PATH}"
rm -rf "${STAGING_DIR}"

# ---- 3. Sign + notarize + staple the DMG ------------------------------------
echo "==> Signing the DMG"
codesign --force --timestamp --sign "${IDENTITY}" "${DMG_PATH}"

notarize "${DMG_PATH}"
echo "==> Stapling the DMG"
xcrun stapler staple "${DMG_PATH}"
xcrun stapler validate "${DMG_PATH}"

# ---- 4. Final Gatekeeper assessment + checksum ------------------------------
echo "==> Gatekeeper assessment of the stapled app"
spctl --assess --type exec --verbose=4 "${APP_PATH}" || {
  echo "error: spctl still rejects the stapled app — investigate before shipping." >&2
  exit 1
}

echo "==> Writing checksum"
( cd "${OUTPUT_DIR}" && shasum -a 256 "$(basename "${DMG_PATH}")" > "$(basename "${DMG_PATH}").sha256" )

echo
echo "Done."
echo "  DMG:      ${DMG_PATH}"
echo "  Checksum: ${DMG_PATH}.sha256"
echo "Send testers the DMG; tell them to drag ScreenCap to Applications and open from there."
