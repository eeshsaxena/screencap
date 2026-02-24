#!/bin/sh
# Install screencap CLI binary.
# Usage: curl -sSf https://storage.googleapis.com/screencap-releases/releases/install.sh | sh
set -eu

INSTALL_DIR="${HOME}/.screencap/bin"
MARKER="# Added by screencap installer"

# Validate HOME
if [ -z "${HOME:-}" ]; then
    echo "ERROR: \$HOME is not set. Aborting." >&2
    exit 1
fi

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64)        ARCH="x86_64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# Distribution base URL (change this when migrating to GitHub Releases or custom domain)
DIST_BASE_URL="${SCREENCAP_DIST_URL:-https://storage.googleapis.com/screencap-releases/releases}"

# Version: env var override or auto-detect from latest.txt in GCS
VERSION="${SCREENCAP_VERSION:-}"
if [ -z "$VERSION" ]; then
    VERSION=$(curl -fsSL "${DIST_BASE_URL}/latest.txt")
    VERSION=$(echo "$VERSION" | tr -d '[:space:]')
    if [ -z "$VERSION" ]; then
        echo "ERROR: Could not detect latest version." >&2
        exit 1
    fi
fi

BASE_URL="${DIST_BASE_URL}/v${VERSION}"
TARBALL="screencap-${VERSION}-macos-${ARCH}.tar.gz"

echo "Installing screencap ${VERSION} for ${ARCH}..."

# Download to temp file (prevents corrupt partial extractions)
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

curl -fsSL "${BASE_URL}/${TARBALL}" -o "$TMPDIR/${TARBALL}"
curl -fsSL "${BASE_URL}/checksums.sha256" -o "$TMPDIR/checksums.sha256"

# Verify checksum BEFORE extraction
if ! (cd "$TMPDIR" && shasum -a 256 -c checksums.sha256 --ignore-missing); then
    echo "ERROR: Checksum verification failed. Aborting." >&2
    exit 1
fi

# Extract
mkdir -p "$INSTALL_DIR"
tar xzf "$TMPDIR/${TARBALL}" -C "$INSTALL_DIR"

# Add to PATH (detect shell, use marker for idempotency)
SHELL_NAME=$(basename "${SHELL:-zsh}")
case "$SHELL_NAME" in
  zsh)  RC_FILE="${HOME}/.zshrc" ;;
  bash) RC_FILE="${HOME}/.bashrc" ;;
  fish) RC_FILE="${HOME}/.config/fish/config.fish" ;;
  *)    RC_FILE="" ;;
esac

if [ -n "$RC_FILE" ] && [ "${SCREENCAP_NO_MODIFY_PATH:-0}" != "1" ]; then
    if ! grep -qF "$MARKER" "$RC_FILE" 2>/dev/null; then
        if [ "$SHELL_NAME" = "fish" ]; then
            echo "set -gx PATH \"$INSTALL_DIR/screencap\" \$PATH $MARKER" >> "$RC_FILE"
        else
            echo "export PATH=\"$INSTALL_DIR/screencap:\$PATH\" $MARKER" >> "$RC_FILE"
        fi
        echo "Added $INSTALL_DIR/screencap to PATH in $RC_FILE"
    fi
elif [ -z "$RC_FILE" ]; then
    echo "Warning: Unknown shell ($SHELL_NAME). Add $INSTALL_DIR/screencap to your PATH manually."
fi

# Verify installation
if "$INSTALL_DIR/screencap/screencap" --version >/dev/null 2>&1; then
    INSTALLED_VERSION=$("$INSTALL_DIR/screencap/screencap" --version 2>&1)
    echo ""
    echo "screencap installed successfully! ($INSTALLED_VERSION)"
else
    echo "ERROR: Installation verification failed." >&2
    exit 1
fi

echo "Run 'screencap --help' to get started."
echo ""
echo "Note: screencap requires Screen Recording and Accessibility"
echo "permissions. You'll be prompted to grant these on first use."
echo ""
echo "IMPORTANT: Because this binary is not yet notarized, you may need to run:"
echo "  xattr -cr ~/.screencap/bin/"
echo "before first use. This will be resolved in a future release."
