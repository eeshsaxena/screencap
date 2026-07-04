#!/usr/bin/env bash
# Tear down macOS DEV-build state for ScreenCap so dev artifacts stop
# masquerading as — or conflicting with — the shipped /Applications install.
#
# WHY THIS EXISTS: dev builds (Xcode Cmd+R, script/build_and_run.sh) drop
# ad-hoc-signed ScreenCap.app copies into DerivedData / macos/.build and
# register the com.screencap.daemon LaunchAgent against a dev identity.
# (Older build_and_run.sh versions also left dev env vars —
# SCREENCAP_DAEMON_USE_DEV_SOURCE, etc. — global in the launchd session; the
# unsetenv step below clears any such leftovers.)
# Spotlight/Launchpad then index EVERY copy under the same name "ScreenCap", so
# launching it can hit an ad-hoc dev build instead of the signed install — which
# fails helper registration ("macOS rejected the helper signature"), shows the
# orange "Ad-hoc dev build" banner, and tangles TCC across the two identities.
# Run this after a dev test session to get back to a clean state where only the
# shipped app exists. See
# docs/solutions/workflow-issues/macos-shipped-vs-dev-build-confusion.md.
#
# SAFE BY DEFAULT:
#   - NEVER touches /Applications/ScreenCap.app (the shipped install).
#   - Moves stray dev app copies to ~/.Trash (reversible) — never `rm`.
#   - TCC reset is OPT-IN (--reset-tcc): the dev and shipped app share bundle id
#     com.screencap.macos, so a blanket reset also drops the SHIPPED app's grants.
#
# Usage:
#   script/clean_dev_macos_state.sh [--dry-run] [--reset-tcc]
#     --dry-run     Print what would change; make no changes.
#     --reset-tcc   Also `tccutil reset` com.screencap.macos (also drops the
#                   shipped app's grants — you re-grant on next launch).

set -euo pipefail

DRY_RUN=0
RESET_TCC=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY_RUN=1 ;;
    --reset-tcc) RESET_TCC=1 ;;
    -h|--help)   sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

SHIPPED="/Applications/ScreenCap.app"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRASH="${HOME}/.Trash"

# Run a command, or just print it under --dry-run. Args are passed through
# verbatim (no eval), so paths with spaces are safe.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

echo "==> ScreenCap dev-state teardown$([ "$DRY_RUN" -eq 1 ] && echo ' (dry-run — no changes)')"

# 1. Quit any running ScreenCap.
echo "==> Quitting ScreenCap (if running)"
if [ "$DRY_RUN" -eq 1 ]; then echo "  [dry-run] pkill -x ScreenCap"; else pkill -x ScreenCap 2>/dev/null || true; fi

# 2. Boot out the daemon LaunchAgent + remove its stale socket. (Re-registers
#    cleanly next time you run the shipped app's setup.)
echo "==> Removing daemon registration + socket"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] launchctl bootout gui/$(id -u)/com.screencap.daemon"
else
  launchctl bootout "gui/$(id -u)/com.screencap.daemon" 2>/dev/null || true
fi
run rm -f "${HOME}/.screencap/run/api.sock"

# 3. Clear dev launchd env pollution (the unambiguous dev-only state).
echo "==> Clearing dev launchd environment variables"
for v in SCREENCAP_CLI_PATH SCREENCAP_DEV_REPO_ROOT SCREENCAP_DEV_PYTHON SCREENCAP_DAEMON_USE_DEV_SOURCE; do
  if [ "$DRY_RUN" -eq 1 ]; then echo "  [dry-run] launchctl unsetenv $v"; else launchctl unsetenv "$v" || true; fi
done

# 4. Move stray dev ScreenCap.app copies to Trash — NEVER the shipped one, and
#    never the Xcode index-only builds (under .noindex, not launchable).
echo "==> Trashing stray dev ScreenCap.app build copies (keeping ${SHIPPED})"
found_stray=0
# Combine Spotlight's view with a direct scan of the build dirs, dedup by path.
{
  mdfind "kMDItemKind == 'Application'" 2>/dev/null | grep -i "ScreenCap.app" || true
  find "${HOME}/Library/Developer/Xcode/DerivedData" "${REPO_ROOT}/macos" \
    -name "ScreenCap.app" -type d 2>/dev/null || true
} | sort -u | while IFS= read -r app; do
  [ -n "$app" ] || continue
  [ -d "$app" ] || continue                       # already moved this run
  [ "$app" = "$SHIPPED" ] && continue             # never the shipped install
  case "$app" in *"/Index.noindex/"*) continue ;; esac   # Xcode index build
  found_stray=1
  hash="$(printf '%s' "$app" | md5 -q 2>/dev/null | cut -c1-8)"
  dest="${TRASH}/ScreenCap-stray-${hash:-$$}.app"
  echo "  stray: $app"
  run mv "$app" "$dest"
done
# (found_stray is set in a subshell above; the per-line "stray:" output is the
# authoritative signal of what was moved.)

# 5. Optional TCC reset — opt-in, because it also drops the SHIPPED app's grants.
if [ "$RESET_TCC" -eq 1 ]; then
  echo "==> Resetting TCC for com.screencap.macos (NOTE: also drops the shipped app's grants)"
  if [ "$DRY_RUN" -eq 1 ]; then echo "  [dry-run] tccutil reset All com.screencap.macos"; else tccutil reset All com.screencap.macos || true; fi
  echo "    The daemon's bare-tool 'screencap' rows (Accessibility / Input Monitoring)"
  echo "    have no bundle id and can't be tccutil-reset — remove them with the '-'"
  echo "    button in each Privacy pane if they linger."
fi

# 6. Report the surviving shipped install so you can confirm it's intact + signed.
echo "==> Surviving shipped install:"
if [ -d "$SHIPPED" ]; then
  team="$(codesign -dvv "$SHIPPED" 2>&1 | sed -n 's/^TeamIdentifier=//p' | head -1)"
  ver="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$SHIPPED/Contents/Info.plist" 2>/dev/null || true)"
  echo "    ${SHIPPED}  (v${ver:-?}, TeamIdentifier=${team:-<none — ad-hoc?!>})"
  if [ -z "$team" ] || [ "$team" = "not set" ]; then
    echo "    WARNING: the /Applications app has no Team Identifier — that's an ad-hoc"
    echo "    build, not the notarized DMG. Re-install from the signed DMG."
  fi
else
  echo "    (none in /Applications — install the notarized DMG)"
fi

echo
echo "Done.$([ "$DRY_RUN" -eq 1 ] && echo ' (dry-run — nothing was changed)')"
echo "Spotlight should now show a single ScreenCap (the shipped install)."
echo "Re-run the app's setup to re-register the daemon cleanly."
