"""Auto-update: check GCS for newer versions and self-replace."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
import requests
from packaging.version import Version
from rich.console import Console
from rich.progress import Progress

from screencap import __version__
from screencap.config import get_auto_update, get_base_dir

_DIST_BASE_URL = os.environ.get(
    "SCREENCAP_DIST_URL",
    "https://storage.googleapis.com/screencap-releases/releases",
)
_VERSION_CHECK_TIMEOUT = (2, 3)  # (connect, read) seconds
_DOWNLOAD_TIMEOUT = 120  # seconds

console = Console()


def get_latest_version() -> str | None:
    """Fetch latest version string from GCS. Returns None on any failure."""
    try:
        resp = requests.get(
            f"{_DIST_BASE_URL}/latest.txt",
            timeout=_VERSION_CHECK_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text.strip()
    except Exception:
        return None


def is_update_available(latest: str) -> bool:
    """Return True if latest is newer than current version."""
    try:
        return Version(latest) > Version(__version__)
    except Exception:
        return False


def _detect_arch() -> str:
    """Return architecture string matching GCS tarball naming."""
    machine = platform.machine()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x86_64"


def _download_and_verify(version: str, dest_dir: Path) -> Path:
    """Download tarball + checksum, verify, extract. Returns extracted dir."""
    arch = _detect_arch()
    tarball_name = f"screencap-{version}-{arch}.tar.gz"
    base_url = f"{_DIST_BASE_URL}/v{version}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        tarball_path = tmp / tarball_name
        checksum_path = tmp / "checksums.sha256"

        # Download tarball with progress bar
        with Progress() as progress:
            resp = requests.get(
                f"{base_url}/{tarball_name}",
                stream=True,
                timeout=_DOWNLOAD_TIMEOUT,
            )
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            task = progress.add_task("Downloading...", total=total)
            with open(tarball_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    progress.update(task, advance=len(chunk))

        # Download checksums
        resp = requests.get(
            f"{base_url}/checksums.sha256",
            timeout=_VERSION_CHECK_TIMEOUT,
        )
        resp.raise_for_status()
        checksum_path.write_text(resp.text)

        # Verify checksum
        expected_hash = None
        for line in checksum_path.read_text().splitlines():
            if tarball_name in line:
                expected_hash = line.split()[0]
                break
        if not expected_hash:
            raise RuntimeError(f"No checksum found for {tarball_name}")

        actual_hash = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Checksum mismatch: expected {expected_hash}, got {actual_hash}"
            )

        # Extract to destination
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.unpack_archive(str(tarball_path), str(dest_dir))

    return dest_dir


def _atomic_swap(install_dir: Path, new_dir: Path) -> None:
    """Atomically swap new binary into place. Keep old as backup."""
    old_backup = install_dir.with_name("screencap-old")

    # Clean up previous backup
    if old_backup.exists():
        shutil.rmtree(old_backup)

    # Atomic swap: rename current -> old, new -> current
    if install_dir.exists():
        install_dir.rename(old_backup)
    new_dir.rename(install_dir)

    # Remove macOS quarantine attributes
    subprocess.run(
        ["xattr", "-cr", str(install_dir)],
        capture_output=True,
    )


def perform_update(version: str) -> bool:
    """Download, verify, and install new version. Returns True on success."""
    install_dir = get_base_dir() / "bin" / "screencap"
    new_dir = get_base_dir() / "bin" / "screencap-new"

    try:
        # Clean up any leftover partial download
        if new_dir.exists():
            shutil.rmtree(new_dir)

        console.print(f"Updating screencap to {version}...")
        _download_and_verify(version, new_dir)
        _atomic_swap(install_dir, new_dir)
        console.print(f"[green]Updated to {version}.[/green]")
        return True

    except KeyboardInterrupt:
        console.print("\n[yellow]Update cancelled.[/yellow]")
        if new_dir.exists():
            shutil.rmtree(new_dir)
        return False

    except Exception as exc:
        console.print(f"[red]Update failed: {exc}[/red]")
        if new_dir.exists():
            shutil.rmtree(new_dir)
        return False


def re_execute() -> None:
    """Replace current process with the (updated) binary."""
    install_dir = get_base_dir() / "bin" / "screencap"
    binary = install_dir / "screencap"
    if binary.exists():
        os.execv(str(binary), sys.argv)


def maybe_check_for_update() -> None:
    """Main entry point: check for update, prompt, install, re-exec."""
    # Guard: frozen binary only
    if not getattr(sys, "frozen", False):
        return

    # Guard: interactive TTY only
    if not sys.stdin.isatty():
        return

    # Guard: config toggle
    if not get_auto_update():
        return

    latest = get_latest_version()
    if latest is None or not is_update_available(latest):
        return

    if click.confirm(
        f"Update available: {__version__} \u2192 {latest}. Update now?",
        default=True,
    ):
        if perform_update(latest):
            re_execute()
            # If re_execute returns (shouldn't), fall through to run command
