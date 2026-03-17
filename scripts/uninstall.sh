#!/bin/sh
# Uninstall screencap CLI.
set -eu

if [ -z "${HOME:-}" ]; then
    echo "ERROR: \$HOME is not set. Aborting." >&2
    exit 1
fi

INSTALL_DIR="${HOME}/.screencap/bin"
VENV_DIR="${HOME}/.screencap/venv"
MARKER="# Added by screencap installer"

# Remove binary / wrapper directory
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo "Removed $INSTALL_DIR"
else
    echo "Nothing to uninstall ($INSTALL_DIR does not exist)."
fi

# Remove pip venv (if present)
if [ -d "$VENV_DIR" ]; then
    rm -rf "$VENV_DIR"
    echo "Removed $VENV_DIR"
fi

# Remove install method marker and pip log
rm -f "${HOME}/.screencap/.install_method"
rm -f "${HOME}/.screencap/install.log"

# Clean up PATH additions from shell configs
for RC_FILE in "${HOME}/.zshrc" "${HOME}/.bashrc" "${HOME}/.config/fish/config.fish"; do
    if [ -f "$RC_FILE" ]; then
        sed -i '' "/$MARKER/d" "$RC_FILE" 2>/dev/null || true
    fi
done

echo "screencap uninstalled."
echo "Note: ~/.screencap/recordings/ and ~/.screencap/config.toml were preserved."
echo "Note: If Python was installed by the screencap installer, it was NOT removed."
echo "      To remove it, use the Python uninstaller in /Applications/Python 3.12/"
