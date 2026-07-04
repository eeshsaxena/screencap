#!/usr/bin/env bash
# Regenerate every derived icon artifact from the branding masters.
#
# Source of truth: macos/branding/masters/ — the delivered brand PNGs
# (light ladder at 16/32/64/128/256/512/1024 px with -2x variants, plus a
# single dark 1024 px master). This script fans them out to:
#
#   1. macos/branding/ScreenCap.icns
#      Assembled from the light masters via a standard .iconset + iconutil.
#      Consumed by the PyInstaller daemon BUNDLE (pyinstaller/screencap.spec)
#      and the release DMG volume icon (script/notarize_app.sh).
#   2. macos/ScreenCap/Assets.xcassets/AppIcon.appiconset/
#      The ten light slot PNGs (copied, renamed -2x -> @2x). The appiconset's
#      Contents.json references these names and is maintained by hand, not by
#      this script.
#
# The dark master (icon_1024x1024_dark.png) is committed but NOT wired: the
# classic appiconset's `luminosity: dark` appearance entries are an iOS-only
# grammar — Xcode 26 actool drops them for the mac idiom as "unassigned
# children" (verified: absent from the compiled Assets.car). Dark mac app
# icons are a macOS 26 feature delivered via an Icon Composer .icon document,
# which needs the mark as a separate layer rather than these baked squircle
# tiles. See the follow-up ticket referenced in the PR that added this file.
#
# Derived outputs are committed and authoritative: no build step invokes this
# script. Re-run it only when the masters change, then commit the results.
#
# Usage: macos/branding/generate-icons.sh

set -euo pipefail

BRANDING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MASTERS_DIR="${BRANDING_DIR}/masters"
APPICONSET_DIR="${BRANDING_DIR}/../ScreenCap/Assets.xcassets/AppIcon.appiconset"
ICNS_PATH="${BRANDING_DIR}/ScreenCap.icns"

# The ten macOS app-icon slots as "size scale" pairs. The delivered masters
# name @2x files with a -2x suffix; Apple's tooling (iconutil, asset catalogs)
# expects @2x, so every consumer below renames explicitly.
SLOTS=(
  "16 1" "16 2"
  "32 1" "32 2"
  "128 1" "128 2"
  "256 1" "256 2"
  "512 1" "512 2"
)

# master_for <size> <scale> -> path of the delivered light PNG for that slot.
master_for() {
  local size="$1" scale="$2"
  if [ "${scale}" = "2" ]; then
    echo "${MASTERS_DIR}/icon_${size}x${size}-2x.png"
  else
    echo "${MASTERS_DIR}/icon_${size}x${size}.png"
  fi
}

# slot_name <size> <scale> -> Apple-conventional basename (no extension).
slot_name() {
  local size="$1" scale="$2"
  if [ "${scale}" = "2" ]; then
    echo "icon_${size}x${size}@2x"
  else
    echo "icon_${size}x${size}"
  fi
}

for pair in "${SLOTS[@]}"; do
  read -r size scale <<<"${pair}"
  master="$(master_for "${size}" "${scale}")"
  [ -f "${master}" ] || { echo "error: missing master ${master}" >&2; exit 1; }
done

# ---- 1. ScreenCap.icns from the light masters --------------------------------
echo "==> Assembling ScreenCap.icns"
ICONSET_DIR="$(mktemp -d)/ScreenCap.iconset"
mkdir -p "${ICONSET_DIR}"
trap 'rm -rf "$(dirname "${ICONSET_DIR}")"' EXIT

for pair in "${SLOTS[@]}"; do
  read -r size scale <<<"${pair}"
  cp "$(master_for "${size}" "${scale}")" \
     "${ICONSET_DIR}/$(slot_name "${size}" "${scale}").png"
done

rm -f "${ICNS_PATH}"
iconutil -c icns "${ICONSET_DIR}" -o "${ICNS_PATH}"
echo "    wrote ${ICNS_PATH}"

# ---- 2. Appiconset light ladder -----------------------------------------------
echo "==> Populating ${APPICONSET_DIR}"
[ -d "${APPICONSET_DIR}" ] || { echo "error: appiconset not found at ${APPICONSET_DIR}" >&2; exit 1; }

for pair in "${SLOTS[@]}"; do
  read -r size scale <<<"${pair}"
  cp "$(master_for "${size}" "${scale}")" \
     "${APPICONSET_DIR}/$(slot_name "${size}" "${scale}").png"
done
echo "    wrote 10 light slot PNGs"

echo "Done. Review and commit the regenerated artifacts."
