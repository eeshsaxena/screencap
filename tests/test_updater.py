"""Tests for the auto-update module."""

import os
import stat
import tarfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from screencap.updater import (
    get_latest_version,
    is_update_available,
    _download_and_verify,
    _atomic_swap,
)


def test_get_latest_version_success():
    mock_resp = MagicMock()
    mock_resp.text = "0.5.0\n"
    mock_resp.raise_for_status = MagicMock()
    with patch("screencap.updater.requests.get", return_value=mock_resp):
        assert get_latest_version() == "0.5.0"


def test_get_latest_version_network_failure():
    with patch("screencap.updater.requests.get", side_effect=Exception("timeout")):
        assert get_latest_version() is None


def test_is_update_available_newer():
    with patch("screencap.updater.__version__", "0.4.1"):
        assert is_update_available("0.5.0") is True


def test_is_update_available_same():
    with patch("screencap.updater.__version__", "0.5.0"):
        assert is_update_available("0.5.0") is False


def test_is_update_available_older():
    with patch("screencap.updater.__version__", "0.5.0"):
        assert is_update_available("0.4.0") is False


def test_is_update_available_malformed():
    assert is_update_available("not-a-version") is False


def test_maybe_check_skipped_when_not_frozen():
    """Update check must not run for pip/dev installs."""
    with patch("screencap.updater.get_latest_version") as mock:
        from screencap.updater import maybe_check_for_update
        maybe_check_for_update()  # sys.frozen is False in test env
        mock.assert_not_called()


def test_update_command_not_frozen():
    """screencap update prints guidance for pip installs."""
    from screencap.cli import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["update"])
    assert "pip install --upgrade" in result.output


def _make_tarball(tarball_path: Path, binary_content: bytes = b"#!/bin/sh\necho hi\n"):
    """Create a tarball mimicking the release structure: screencap/screencap inside."""
    with tarfile.open(tarball_path, "w:gz") as tar:
        import io

        # Add the inner directory entry
        dir_info = tarfile.TarInfo(name="screencap")
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o755
        tar.addfile(dir_info)

        # Add the binary inside the directory
        bin_info = tarfile.TarInfo(name="screencap/screencap")
        bin_info.size = len(binary_content)
        bin_info.mode = 0o755
        tar.addfile(bin_info, io.BytesIO(binary_content))


def test_download_and_verify_flattens_nested_dir(tmp_path):
    """After extract + swap, binary must be at install_dir/screencap, not deeper."""
    import hashlib

    version = "0.7.0"
    arch = "arm64"
    tarball_name = f"screencap-{version}-{arch}.tar.gz"
    binary_content = b"#!/bin/sh\necho hello\n"

    # Build the tarball on disk
    tarball_path = tmp_path / tarball_name
    _make_tarball(tarball_path, binary_content)

    # Compute its checksum
    sha = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
    checksums_text = f"{sha}  {tarball_name}\n"

    # Mock requests.get to serve the tarball and checksums from disk
    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith(".tar.gz"):
            resp.headers = {"content-length": str(tarball_path.stat().st_size)}
            resp.iter_content = lambda chunk_size: iter(
                [tarball_path.read_bytes()]
            )
        else:
            resp.text = checksums_text
        return resp

    dest_dir = tmp_path / "screencap-new"
    install_dir = tmp_path / "screencap"

    with (
        patch("screencap.updater.requests.get", side_effect=fake_get),
        patch("screencap.updater._detect_arch", return_value=arch),
    ):
        _download_and_verify(version, dest_dir)

    # Before swap: binary should be at dest_dir/screencap (not dest_dir/screencap/screencap)
    assert (dest_dir / "screencap").is_file(), (
        f"Expected binary at {dest_dir}/screencap, "
        f"found: {list(dest_dir.rglob('*'))}"
    )

    # Perform the atomic swap (mock xattr since it's macOS-only)
    with patch("screencap.updater.subprocess.run"):
        _atomic_swap(install_dir, dest_dir)

    # After swap: binary at install_dir/screencap
    binary = install_dir / "screencap"
    assert binary.is_file()
    assert binary.read_bytes() == binary_content

    # Binary must be executable
    assert binary.stat().st_mode & stat.S_IXUSR
