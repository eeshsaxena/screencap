"""Tests for the shared atomic-write primitive.

``atomic_write_0600`` promises a complete-or-old result at mode 0600. These
pin the key correctness point: it must survive a *short* ``os.write`` (which
may write fewer bytes than requested for a large buffer) without leaving a
truncated file, and on any failure it must clean up its temp file and leave
the destination untouched.
"""

from __future__ import annotations

import os
import stat

import pytest

from screencap.atomic_io import atomic_write_0600


def test_writes_full_content_at_mode_0600(tmp_path):
    dest = tmp_path / "out.bin"
    data = b"hello world\n" * 5000
    atomic_write_0600(dest, data)
    assert dest.read_bytes() == data
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.part")) == []  # temp promoted, none left behind


def test_survives_short_os_write(tmp_path, monkeypatch):
    """A capped os.write (simulating a short write on a large buffer) must not
    truncate the result; a single unchecked os.write() would."""
    real_write = os.write

    def capped_write(fd, buf):
        # Write at most 7 bytes per call, forcing the write loop to iterate.
        return real_write(fd, bytes(buf)[:7])

    monkeypatch.setattr(os, "write", capped_write)
    dest = tmp_path / "out.bin"
    data = bytes(range(256)) * 64  # 16 KiB, far larger than the 7-byte cap
    atomic_write_0600(dest, data, fsync=False)
    assert dest.read_bytes() == data


def test_empty_data_writes_empty_file(tmp_path):
    dest = tmp_path / "empty.bin"
    atomic_write_0600(dest, b"")
    assert dest.read_bytes() == b""


def test_cleans_up_and_leaves_dest_untouched_on_error(tmp_path, monkeypatch):
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"original")

    def failing_write(fd, buf):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError):
        atomic_write_0600(dest, b"new contents")

    assert dest.read_bytes() == b"original"  # untouched, replace never ran
    assert list(tmp_path.glob("*.part")) == []  # temp file cleaned up
