#!/bin/sh
# Install screencap CLI.
# Usage: curl -sSfL https://get.screencap.sh | sh
#
# Tries the pre-built binary first. If it is incompatible with the host
# macOS version, automatically falls back to a pip-based install from source.
set -eu

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INSTALL_DIR="${HOME}/.screencap/bin"
MARKER="# Added by screencap installer"
VENV_DIR="${HOME}/.screencap/venv"
PIP_LOG="${HOME}/.screencap/install.log"
INSTALL_METHOD=""

# Python installer — update URL + SHA-256 when bumping Python version.
PYTHON_VERSION="3.12.10"
PYTHON_PKG_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-macos11.pkg"
PYTHON_PKG_SHA256="8373e58da4ea146b3eb1c1f9834f19a319440b6b679b06050b1f9ee3237aa8e4"

# ---------------------------------------------------------------------------
# Validate environment
# ---------------------------------------------------------------------------
if [ -z "${HOME:-}" ]; then
    echo "ERROR: \$HOME is not set. Aborting." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Detect architecture + Rosetta 2
# ---------------------------------------------------------------------------
ARCH=$(uname -m)
case "$ARCH" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64)        ARCH="x86_64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# Rosetta 2: x86_64 process on arm64 hardware — use native arm64 binary
if [ "$ARCH" = "x86_64" ]; then
    if sysctl -n sysctl.proc_translated 2>/dev/null | grep -q 1; then
        echo "Detected Rosetta 2 — using native arm64 binary instead."
        ARCH="arm64"
    fi
fi

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

# Find a usable Python 3.10+, skipping the Xcode CLT shim that pops a
# GUI dialog when no real Python is installed.
find_python() {
    for cmd in python3 python3.12 python3.11 python3.10; do
        if command -v "$cmd" >/dev/null 2>&1; then
            # The CLT shim cannot execute real Python code — this filters it out
            if "$cmd" -c "import sys; sys.exit(0)" 2>/dev/null; then
                py_ver=$("$cmd" -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))")
                py_major=${py_ver%.*}
                py_minor=${py_ver#*.}
                if [ "$py_major" -ge 3 ] && [ "$py_minor" -ge 10 ]; then
                    echo "$cmd"
                    return 0
                fi
            fi
        fi
    done
    return 1
}

# Refuse to continue on macOS < 11 — av/PyAV wheels need macosx_11_0
check_macos_version() {
    macos_ver=$(sw_vers -productVersion 2>/dev/null || echo "0.0")
    macos_major=$(echo "$macos_ver" | cut -d. -f1)
    if [ "$macos_major" -lt 11 ] 2>/dev/null; then
        echo "ERROR: macOS 11 (Big Sur) or later is required for source installation." >&2
        echo "Your version: macOS $macos_ver" >&2
        echo "The pre-built binary requires macOS 14+." >&2
        echo "Please update macOS to continue." >&2
        exit 1
    fi
}

# Download, verify, and install the python.org macOS .pkg
install_python_pkg() {
    echo "Python 3.10+ is not installed."
    echo "Downloading Python ${PYTHON_VERSION} from python.org..."

    curl -fsSL "$PYTHON_PKG_URL" -o "$SCRATCH_DIR/python.pkg"

    # Verify SHA-256
    actual_sha=$(shasum -a 256 "$SCRATCH_DIR/python.pkg" | cut -d' ' -f1)
    if [ "$actual_sha" != "$PYTHON_PKG_SHA256" ]; then
        echo "ERROR: Python installer checksum mismatch." >&2
        echo "Expected: $PYTHON_PKG_SHA256" >&2
        echo "Got:      $actual_sha" >&2
        exit 1
    fi

    # Verify Apple code signature
    if ! pkgutil --check-signature "$SCRATCH_DIR/python.pkg" >/dev/null 2>&1; then
        echo "ERROR: Python installer code signature verification failed." >&2
        exit 1
    fi

    echo ""
    echo "This will install Python ${PYTHON_VERSION} system-wide (requires admin password)."
    echo ""
    sudo installer -pkg "$SCRATCH_DIR/python.pkg" -target /
}

# Full pip-based install fallback
install_via_pip() {
    check_macos_version

    # Native-extension compilation requires Xcode Command Line Tools
    if ! xcode-select -p >/dev/null 2>&1; then
        echo "Xcode Command Line Tools are required but not installed."
        echo "Please run this command, then re-run the screencap installer:"
        echo ""
        echo "  xcode-select --install"
        echo ""
        exit 1
    fi

    # Find or install Python
    PYTHON=$(find_python || true)
    if [ -z "$PYTHON" ]; then
        install_python_pkg
        PYTHON=$(find_python || true)
        if [ -z "$PYTHON" ]; then
            echo "ERROR: Python 3.10+ still not found after installation." >&2
            echo "Please install Python 3.10+ manually and re-run." >&2
            exit 1
        fi
    fi

    echo "Using $PYTHON ($("$PYTHON" --version 2>&1))"

    # Create (or recreate) venv — rm first so re-running the script is a clean retry
    mkdir -p "${HOME}/.screencap"
    rm -rf "$VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"

    # Fresh log for this install attempt
    : > "$PIP_LOG"

    # Upgrade pip inside venv
    "$VENV_DIR/bin/pip" install --upgrade pip >>"$PIP_LOG" 2>&1

    # Install screencap with recording dependencies
    echo "Installing screencap (this may take a few minutes)..."
    if ! "$VENV_DIR/bin/pip" install "screencap[record]==${VERSION}" >>"$PIP_LOG" 2>&1; then
        echo "" >&2
        echo "ERROR: Installation failed. See $PIP_LOG for details." >&2
        echo "Common fix: install Xcode Command Line Tools with:" >&2
        echo "  xcode-select --install" >&2
        exit 1
    fi

    # Wrapper script — reuses the same PATH entry as the binary install.
    # A wrapper (not a symlink) survives venv rebuilds.
    mkdir -p "$INSTALL_DIR/screencap"
    cat > "$INSTALL_DIR/screencap/screencap" << 'WRAPPER'
#!/bin/sh
exec "$HOME/.screencap/venv/bin/screencap" "$@"
WRAPPER
    chmod +x "$INSTALL_DIR/screencap/screencap"

    # Record install method
    INSTALL_METHOD="pip"
    echo "$INSTALL_METHOD" > "${HOME}/.screencap/.install_method"

    # Verify pip install
    verify_out=$("$INSTALL_DIR/screencap/screencap" --version 2>&1) && verify_rc=0 || verify_rc=$?
    if [ "$verify_rc" -eq 0 ]; then
        echo ""
        echo "screencap installed successfully! ($verify_out)"
    else
        echo "ERROR: Installation verification failed after pip install." >&2
        echo "Check $PIP_LOG for details." >&2
        exit 1
    fi
}

# Add INSTALL_DIR/screencap to shell PATH (idempotent via marker comment)
add_to_path() {
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
}

# ---------------------------------------------------------------------------
# Resolve version
# ---------------------------------------------------------------------------
DIST_BASE_URL="${SCREENCAP_DIST_URL:-https://storage.googleapis.com/screencap-releases/releases}"

VERSION="${SCREENCAP_VERSION:-}"
if [ -z "$VERSION" ]; then
    VERSION=$(curl -fsSL "${DIST_BASE_URL}/latest.txt")
    VERSION=$(echo "$VERSION" | tr -d '[:space:]')
    if [ -z "$VERSION" ]; then
        echo "ERROR: Could not detect latest version." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Scratch directory (cleaned up on exit)
# ---------------------------------------------------------------------------
SCRATCH_DIR=$(mktemp -d)
trap 'rm -rf "$SCRATCH_DIR"' EXIT

# ---------------------------------------------------------------------------
# PATH setup (both install methods use the same directory)
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
add_to_path

# ---------------------------------------------------------------------------
# Install method override for testing (SCREENCAP_INSTALL_METHOD=pip|binary)
# ---------------------------------------------------------------------------
INSTALL_METHOD_OVERRIDE="${SCREENCAP_INSTALL_METHOD:-}"

if [ "$INSTALL_METHOD_OVERRIDE" = "pip" ]; then
    echo "SCREENCAP_INSTALL_METHOD=pip — skipping binary, installing from source..."
    install_via_pip
    echo "Run 'screencap --help' to get started."
    echo ""
    echo "Note: screencap requires Screen Recording and Accessibility"
    echo "permissions. You'll be prompted to grant these on first use."
    exit 0
fi

# ---------------------------------------------------------------------------
# Binary install attempt
# ---------------------------------------------------------------------------
BASE_URL="${DIST_BASE_URL}/v${VERSION}"
TARBALL="screencap-${VERSION}-${ARCH}.tar.gz"

echo "Installing screencap ${VERSION} for ${ARCH}..."

curl -fsSL "${BASE_URL}/${TARBALL}" -o "$SCRATCH_DIR/${TARBALL}"
curl -fsSL "${BASE_URL}/checksums.sha256" -o "$SCRATCH_DIR/checksums.sha256"

# Verify checksum BEFORE extraction
if ! (cd "$SCRATCH_DIR" && shasum -a 256 -c checksums.sha256 --ignore-missing); then
    echo "ERROR: Checksum verification failed. Aborting." >&2
    exit 1
fi

# Extract (remove previous install to avoid "Can't replace directory" errors)
rm -rf "$INSTALL_DIR/screencap"
tar xzf "$SCRATCH_DIR/${TARBALL}" -C "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# Verify binary — fall back to pip if incompatible
# ---------------------------------------------------------------------------
VERIFY_OUTPUT=$("$INSTALL_DIR/screencap/screencap" --version 2>&1) && VERIFY_EXIT=0 || VERIFY_EXIT=$?

if [ "$VERIFY_EXIT" -eq 0 ]; then
    INSTALL_METHOD="binary"
    mkdir -p "${HOME}/.screencap"
    echo "$INSTALL_METHOD" > "${HOME}/.screencap/.install_method"
    echo ""
    echo "screencap installed successfully! ($VERIFY_OUTPUT)"
else
    # SCREENCAP_INSTALL_METHOD=binary means binary-only, no fallback
    if [ "$INSTALL_METHOD_OVERRIDE" = "binary" ]; then
        echo "ERROR: Installation verification failed." >&2
        if [ -n "$VERIFY_OUTPUT" ]; then
            echo "Detail: $VERIFY_OUTPUT" >&2
        else
            echo "The binary exited without output. This usually means your macOS" >&2
            echo "version is older than what the binary was built for." >&2
        fi
        exit 1
    fi

    echo ""
    echo "The pre-built binary is not compatible with your system."
    echo "Installing from source instead (this may take a few minutes)..."
    echo ""
    # Clean up failed binary before pip fallback
    rm -rf "$INSTALL_DIR/screencap"
    install_via_pip
fi

# ---------------------------------------------------------------------------
# Final messaging
# ---------------------------------------------------------------------------
echo "Run 'screencap --help' to get started."
echo ""
echo "Note: screencap requires Screen Recording and Accessibility"
echo "permissions. You'll be prompted to grant these on first use."

if [ "$INSTALL_METHOD" = "binary" ]; then
    echo ""
    echo "IMPORTANT: Because this binary is not yet notarized, you may need to run:"
    echo "  xattr -cr ~/.screencap/bin/"
    echo "before first use. This will be resolved in a future release."
fi
