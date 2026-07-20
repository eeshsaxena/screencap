#!/usr/bin/env bash
# what-needs-releasing.sh — release preflight for Screencap's two tracks.
#
# Screencap ships two separately-versioned, independently-released artifacts:
#
#   • CLI / daemon (the engine)  — tag `vX.Y.Z`, consumed by the headless
#     `install.sh` / `~/.screencap/bin` channel. Released via the `local-release`
#     skill or the `release` CI flow.
#   • macOS app (the .app/.dmg)  — tag `macos-app-vX.Y.Z`, a local-only
#     signed+notarized DMG. Released via the `macos-app-release` skill.
#
# The app EMBEDS a daemon built from the current Python source at app-build time,
# so an app release also ships any `src/screencap` change since the last app tag —
# whereas the headless CLI channel only advances when a `vX.Y.Z` tag is cut.
#
# This script diffs HEAD against the last tag of each track and prints, for each,
# whether a release is warranted and which files drive it. It reads only local
# git state (run `git fetch --tags` first if you want to be current). It never
# mutates anything.
#
# Usage:  bash scripts/what-needs-releasing.sh

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }

# --- resolve the latest tag on each track -----------------------------------
# CLI tags are vX.Y.Z; the app tags are macos-app-vX.Y.Z. `--sort=-v:refname`
# orders by embedded version, so the app pattern must be excluded from the CLI
# lookup (macos-app-v0.1.5 would otherwise sort in among the v* tags).
last_cli_tag="$(git tag --list 'v*' --sort=-v:refname | head -1 || true)"
last_app_tag="$(git tag --list 'macos-app-v*' --sort=-v:refname | head -1 || true)"

# Paths that, when changed, warrant a CLI (headless) release.
cli_paths=('src/screencap' 'pyproject.toml' 'pyinstaller' 'scripts/install.sh')
# Paths that warrant a macOS app release. The app bundles the daemon, so a change
# to the embedded engine source (src/screencap, pyproject) also justifies an app
# rebuild in addition to any macos/ (Swift/UI) change.
app_paths=('macos' 'src/screencap' 'pyproject.toml' 'pyinstaller')

report_track() {
  local label="$1" tag="$2"; shift 2
  local paths=("$@")
  bold "== ${label} =="
  if [ -z "${tag}" ]; then
    echo "  no prior tag found — first release would ship everything."
    echo
    return
  fi
  local total touched
  total="$(git rev-list --count "${tag}..HEAD")"
  touched="$(git diff --name-only "${tag}..HEAD" -- "${paths[@]}" | wc -l | tr -d ' ')"
  echo "  last release:      ${tag} ($(git log -1 --format=%cs "${tag}^{commit}"))"
  echo "  commits since:     ${total} total, ${touched} touching release-relevant paths"
  if [ "${touched}" -gt 0 ]; then
    printf '  \033[32mVERDICT: release warranted.\033[0m\n'
    echo "  files driving it (since ${tag}):"
    git diff --name-only "${tag}..HEAD" -- "${paths[@]}" | sed 's/^/    /' | head -40
    local more
    more="$(git diff --name-only "${tag}..HEAD" -- "${paths[@]}" | tail -n +41 | wc -l | tr -d ' ')"
    [ "${more}" -gt 0 ] && dim "    … and ${more} more"
  else
    printf '  \033[2mVERDICT: no release-relevant changes.\033[0m\n'
  fi
  echo
}

echo
report_track "CLI / daemon (headless install.sh channel — tag vX.Y.Z)" "${last_cli_tag}" "${cli_paths[@]}"
report_track "macOS app (.dmg — tag macos-app-vX.Y.Z)" "${last_app_tag}" "${app_paths[@]}"

cat <<'EOF'
Next step:
  • CLI / daemon needs a release → run the `local-release` skill (or `release` CI).
  • macOS app needs a release     → run the `macos-app-release` skill.
  • Both                          → release the CLI first so the app embeds a
                                    published daemon version, then the app.
EOF
