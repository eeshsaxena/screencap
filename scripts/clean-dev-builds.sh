#!/usr/bin/env bash
#
# clean-dev-builds.sh — sweep stray ScreenCap dev/build artifacts so the only
# ScreenCap.app left on the machine is the installed /Applications one.
#
# WHY THIS EXISTS
#   Every git worktree that builds the macOS app makes Xcode create a *new*
#   ~/Library/Developer/Xcode/DerivedData/ScreenCap-<hash> tree (the hash is
#   derived from the project's absolute path). When the worktree is removed the
#   DerivedData is orphaned but never cleaned, and macOS 26's Apps window indexes
#   every .app on disk — so those leftover Debug/Release bundles show up as
#   duplicate "ScreenCap" icons and the daemon bundle shows up as "ScreencapDaemon".
#   This script removes those strays and re-registers the real app with Launch
#   Services so the ghosts disappear.
#
# SAFETY
#   * NEVER touches /Applications/ScreenCap.app (the installed / released app).
#   * A DerivedData folder is only deleted whole when its recorded WorkspacePath
#     no longer exists (i.e. its worktree was removed) — so a worktree you are
#     actively building in Xcode is never nuked. Pass --deep to override.
#   * Stray .app *bundles* outside /Applications are always removed (a dev build
#     is never the thing you ship-test); for a still-live worktree only the built
#     bundle is removed, the incremental build cache is kept.
#
# USAGE
#   scripts/clean-dev-builds.sh              # normal sweep
#   scripts/clean-dev-builds.sh --dry-run    # show what would be removed
#   scripts/clean-dev-builds.sh --deep       # also remove ALL ScreenCap DerivedData
#
set -euo pipefail

DRY_RUN=0
DEEP=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --deep)    DEEP=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DD="$HOME/Library/Developer/Xcode/DerivedData"
LSR="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
KEEP="/Applications/ScreenCap.app"

removed=0
freed_note=""

say()  { printf '%s\n' "$*"; }
act()  { # act <human-msg> <cmd...>
  local msg="$1"; shift
  if [[ "$DRY_RUN" == 1 ]]; then
    say "  [dry-run] would: $msg"
  else
    "$@" && say "  removed: $msg" || say "  WARN could not remove: $msg"
  fi
}

unregister() { [[ -x "$LSR" && -e "$1" ]] && "$LSR" -u "$1" >/dev/null 2>&1 || true; }

say "==> ScreenCap dev-build sweep$([[ "$DRY_RUN" == 1 ]] && echo ' (dry-run)')"
say "    keeping: $KEEP"

# 1. Orphaned / live DerivedData -------------------------------------------------
if [[ -d "$DD" ]]; then
  for d in "$DD"/ScreenCap-*; do
    [[ -d "$d" ]] || continue
    wp="$(/usr/libexec/PlistBuddy -c 'Print WorkspacePath' "$d/info.plist" 2>/dev/null || true)"
    stale=0
    { [[ "$DEEP" == 1 ]] || [[ -z "$wp" ]] || [[ ! -e "$wp" ]]; } && stale=1
    # unregister any .app bundles inside before deleting
    while IFS= read -r app; do unregister "$app"; done \
      < <(find "$d/Build/Products" -maxdepth 2 -iname 'ScreenCap*.app' -prune 2>/dev/null || true)
    if [[ "$stale" == 1 ]]; then
      act "orphaned DerivedData $(basename "$d")  (src: ${wp:-unknown})" rm -rf "$d"
      removed=$((removed+1))
    else
      # live worktree: drop only the built bundles, keep incremental cache
      while IFS= read -r app; do
        act "built bundle in live worktree cache ($app)" rm -rf "$app"
        removed=$((removed+1))
      done < <(find "$d/Build/Products" -maxdepth 2 -iname 'ScreenCap*.app' -prune 2>/dev/null || true)
    fi
  done
fi

# 2. Repo build outputs ----------------------------------------------------------
for p in \
  "$REPO/dist/ScreencapDaemon.app" \
  "$REPO/dist/screencap" \
  "$REPO/pyinstaller/dist/screencap" \
  "$REPO/pyinstaller/dist/ScreencapDaemon.app" ; do
  [[ -e "$p" ]] && { unregister "$p"; act "repo build output ($p)" rm -rf "$p"; removed=$((removed+1)); }
done
# .build / in-repo DerivedData bundles (e.g. macos/.build/ReleaseDD, macos/DerivedData)
while IFS= read -r app; do
  unregister "$app"; act "in-repo bundle ($app)" rm -rf "$app"; removed=$((removed+1))
done < <(find "$REPO/macos" \( -name .build -o -name DerivedData \) -type d -prune -exec \
          find {} -iname 'ScreenCap*.app' -prune \; 2>/dev/null || true)

# 3. Catch-all: any other ScreenCap*.app outside /Applications -------------------
while IFS= read -r app; do
  [[ "$app" == "$KEEP" ]] && continue
  case "$app" in "$DD"/*|"$REPO"/*) continue ;; esac   # already handled above
  unregister "$app"; act "stray bundle ($app)" rm -rf "$app"; removed=$((removed+1))
done < <(mdfind -name 'ScreenCap.app' 2>/dev/null || true)

# 4. Re-register the real app so Launch Services drops the ghosts ----------------
if [[ "$DRY_RUN" != 1 && -x "$LSR" && -d "$KEEP" ]]; then
  "$LSR" -f "$KEEP" >/dev/null 2>&1 || true
  say "==> re-registered $KEEP with Launch Services"
fi

say "==> done — $removed stray item(s) $([[ "$DRY_RUN" == 1 ]] && echo 'would be ' )removed"
if [[ "$DRY_RUN" != 1 && "$removed" -gt 0 ]]; then
  say "    (if duplicate icons linger in the Apps window, log out/in or reboot to flush the icon cache)"
fi
