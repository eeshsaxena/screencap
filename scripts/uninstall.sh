#!/bin/sh
# Uninstall screencap CLI binary.
set -eu

if [ -z "${HOME:-}" ]; then
    echo "ERROR: \$HOME is not set. Aborting." >&2
    exit 1
fi

INSTALL_DIR="${HOME}/.screencap/bin"
MARKER="# Added by screencap installer"

# Remove installation directory
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo "Removed $INSTALL_DIR"
else
    echo "Nothing to uninstall ($INSTALL_DIR does not exist)."
fi

# Clean up PATH additions from shell configs
for RC_FILE in "${HOME}/.zshrc" "${HOME}/.bashrc" "${HOME}/.config/fish/config.fish"; do
    if [ -f "$RC_FILE" ]; then
        sed -i '' "/$MARKER/d" "$RC_FILE" 2>/dev/null || true
    fi
done

echo "screencap uninstalled."
echo "Note: ~/.screencap/recordings/ and ~/.screencap/config.toml were preserved."
