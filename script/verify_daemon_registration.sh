#!/usr/bin/env bash
# Verify a built Screencap.app satisfies every precondition macOS's SMAppService
# LaunchAgent registration needs to SUCCEED — so a release can never ship a bundle
# that dead-ends first-run onboarding at "macos rejected the helper signature" or
# "the helper plist was not found".
#
# The app registers its background daemon with:
#
#     SMAppService.agent(plistName: "com.screencap.daemon.plist").register()
#
# (macos/Screencap/Controllers/DaemonInstallController.swift). That call FAILS —
# blocking every user at the permissions step, with the Screen Recording /
# Accessibility rows locked on "approve the helper above first" — when any of the
# following is wrong in the shipped bundle. Xcode + sign_app.sh normally get them
# right, but each is produced by a *separate* build step (a post-build copy phase,
# an inside-out re-sign) that can silently drift, and none of them is otherwise
# gated. This script asserts them directly on the artifact:
#
#   1. The LaunchAgent plist is present. Absent → SMAppService status .notFound →
#      DaemonInstallController maps it to .installFailed(.plistWriteFailed)
#      ("the helper plist was not found in the app bundle").
#   2. Its Label equals the plist filename base. SMAppService resolves the job by
#      the plist filename; a Label that disagrees with the identifier the rest of
#      the system (bootout/kickstart) uses breaks recovery.
#   3. Its BundleProgram points at an executable that exists in the bundle. Missing
#      → launchd can't exec the daemon; the helper never answers and onboarding
#      hangs at .polling / .daemonDidNotStart.
#   4. The app AND the embedded helper are BOTH Developer-ID (team) signed — not
#      ad-hoc — with the SAME Team Identifier. An ad-hoc or cross-team LaunchAgent
#      is rejected with kSMErrorInvalidSignature →
#      .installFailed(.daemonSigningInvalid) ("macos rejected the helper
#      signature"). This is the exact failure an ad-hoc dev build shows.
#
# Read-only: only PlistBuddy/codesign READS, no signing, no mutation. Safe to run
# against a signed-and-stapled app, against the app inside a built DMG, in CI, or
# ad hoc. Exit 0 = every precondition holds.
#
# Usage:
#   script/verify_daemon_registration.sh path/to/Screencap.app
#
# Invoked automatically by script/sign_app.sh at the end of the release re-sign.

set -euo pipefail

# --- Source-of-truth constants -----------------------------------------------
# These MUST stay in lockstep with the Swift/plist source. The plist-contract
# unit test (macos/ScreencapTests/DaemonRegistrationContractTests.swift) pins the
# same values against DaemonInstallController.plistName + the committed plist, so
# a source-side refactor is caught in CI before a build ever reaches this gate.
EXPECTED_PLIST_NAME="com.screencap.daemon.plist"   # == DaemonInstallController.plistName
EXPECTED_LABEL="com.screencap.daemon"              # == plist :Label (and the launchd job label)

fail() { echo "error: $*" >&2; exit 1; }

APP_PATH="${1:-}"
if [ -z "${APP_PATH}" ]; then
  echo "usage: $0 <path-to-.app>" >&2
  exit 2
fi
[ -d "${APP_PATH}" ] || fail "${APP_PATH} is not a directory (.app bundle expected)."

echo "==> Verifying daemon-registration preconditions for ${APP_PATH}"

# --- 1. LaunchAgent plist present --------------------------------------------
PLIST="${APP_PATH}/Contents/Library/LaunchAgents/${EXPECTED_PLIST_NAME}"
[ -f "${PLIST}" ] || fail "missing LaunchAgent plist at Contents/Library/LaunchAgents/${EXPECTED_PLIST_NAME}. \
SMAppService.agent(plistName:) would report .notFound → .plistWriteFailed for every user. \
The app target's 'Embed daemon LaunchAgent' post-build copy did not run or was removed."

# --- 2. Label matches the plist filename base --------------------------------
[ "${EXPECTED_LABEL}.plist" = "${EXPECTED_PLIST_NAME}" ] \
  || fail "internal: EXPECTED_LABEL (${EXPECTED_LABEL}) and EXPECTED_PLIST_NAME (${EXPECTED_PLIST_NAME}) disagree."
LABEL="$(/usr/libexec/PlistBuddy -c 'Print :Label' "${PLIST}" 2>/dev/null || true)"
[ "${LABEL}" = "${EXPECTED_LABEL}" ] \
  || fail "LaunchAgent plist :Label is '${LABEL}', expected '${EXPECTED_LABEL}'."

# --- 3. BundleProgram exists and is executable -------------------------------
# BundleProgram is resolved relative to the .app bundle root.
PROGRAM="$(/usr/libexec/PlistBuddy -c 'Print :BundleProgram' "${PLIST}" 2>/dev/null || true)"
[ -n "${PROGRAM}" ] || fail "LaunchAgent plist has no :BundleProgram key."
PROGRAM_PATH="${APP_PATH}/${PROGRAM}"
[ -x "${PROGRAM_PATH}" ] \
  || fail "BundleProgram '${PROGRAM}' is not an executable file at ${PROGRAM_PATH}. \
launchd cannot exec the daemon; the helper never answers."

# --- 4. App + helper both team-signed (not ad-hoc) with a MATCHING team ------
HELPER="${APP_PATH}/Contents/Library/LoginItems/ScreencapDaemon.app"
[ -d "${HELPER}" ] || fail "embedded daemon helper missing at Contents/Library/LoginItems/ScreencapDaemon.app."

# codesign prints `TeamIdentifier=<TEAM>` for a team-signed binary and either omits
# the line or prints `TeamIdentifier=not set` for an ad-hoc one. `codesign -dv`
# exits non-zero on an unsigned/ad-hoc target; capture with `|| true` so the empty
# result flows into the is_team check below (which prints the actionable error)
# rather than aborting the script bare under `set -euo pipefail`.
team_id() {
  local out
  out="$(codesign -dv --verbose=4 "$1" 2>&1 || true)"
  printf '%s\n' "${out}" | sed -n 's/^TeamIdentifier=//p' | head -n 1
}
APP_TEAM="$(team_id "${APP_PATH}")"
HELPER_TEAM="$(team_id "${HELPER}")"

is_team() { [ -n "$1" ] && [ "$1" != "not set" ]; }
is_team "${APP_TEAM}" \
  || fail "app is ad-hoc signed (no Team Identifier). SMAppService rejects an ad-hoc \
LaunchAgent with kSMErrorInvalidSignature → 'macos rejected the helper signature'. \
Re-sign with a Developer ID Application identity (script/sign_app.sh)."
is_team "${HELPER_TEAM}" \
  || fail "embedded daemon helper is ad-hoc signed (no Team Identifier)."
[ "${APP_TEAM}" = "${HELPER_TEAM}" ] \
  || fail "app Team Identifier (${APP_TEAM}) != helper Team Identifier (${HELPER_TEAM}). \
SMAppService requires the LaunchAgent and the registering app to share one identity."

echo "==> Daemon-registration preconditions OK:"
echo "      plist   = Contents/Library/LaunchAgents/${EXPECTED_PLIST_NAME}"
echo "      label   = ${LABEL}"
echo "      program = ${PROGRAM}"
echo "      team    = ${APP_TEAM} (app == helper)"
