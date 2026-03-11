#!/usr/bin/env bash
# Download and process UT1 blocklist categories for screencap privacy.
#
# Source: https://github.com/olbat/ut1-blacklists (mirror of Université Toulouse)
# License: CC BY-SA 4.0
#
# Usage:  ./scripts/update-ut1.sh
#
# Idempotent — safe to re-run.  Overwrites existing files in-place.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/src/screencap/privacy/data/ut1"
BASE_URL="https://raw.githubusercontent.com/olbat/ut1-blacklists/master/blacklists"

CATEGORIES=(bank financial webmail social_networks chat vpn)

mkdir -p "$OUT_DIR"

is_ip() {
    # Match IPv4 or IPv6 (starts with digit and contains dots/colons only)
    [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ ]] || [[ "$1" =~ ^[0-9a-fA-F]*: ]]
}

is_noise() {
    local line="$1"
    # Skip blank, comments, IPs, lines with spaces/slashes (paths, not domains)
    [[ -z "$line" ]] && return 0
    [[ "$line" == \#* ]] && return 0
    is_ip "$line" && return 0
    [[ "$line" == *" "* ]] && return 0
    [[ "$line" == *"/"* ]] && return 0
    # Must contain at least one dot (valid domain)
    [[ "$line" != *.* ]] && return 0
    return 1
}

for cat in "${CATEGORIES[@]}"; do
    url="$BASE_URL/$cat/domains"
    out_file="$OUT_DIR/$cat.txt"
    echo "Downloading $cat ..."
    raw=$(curl -fsSL "$url") || { echo "WARN: failed to download $cat, skipping"; continue; }

    # Filter: lowercase, strip trailing dots, remove IPs and noise
    filtered=""
    while IFS= read -r line; do
        line="${line%%$'\r'}"          # strip CR
        line="${line,,}"               # lowercase
        line="${line%.}"               # strip trailing dot
        line="${line## }"              # trim leading space
        line="${line%% }"              # trim trailing space
        if is_noise "$line"; then
            continue
        fi
        filtered+="$line"$'\n'
    done <<< "$raw"

    # Sort, deduplicate, write
    echo "$filtered" | sort -u | sed '/^$/d' > "$out_file"
    count=$(wc -l < "$out_file" | tr -d ' ')
    echo "  -> $out_file ($count domains)"
done

# Write VERSION
echo "$(date -u +%Y-%m-%d)" > "$OUT_DIR/VERSION"
echo "Done. VERSION set to $(cat "$OUT_DIR/VERSION")"
