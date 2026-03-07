"""Tests for screencap.chunk_processor — ChunkProcessor pipeline."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def capture_dir(tmp_path):
    """Create a minimal capture directory."""
    db_path = tmp_path / "recording.db"
    # Create minimal SQLite DB with action_event table
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE action_event (id INTEGER PRIMARY KEY, timestamp REAL, type TEXT, data TEXT)"
    )
    conn.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    conn.execute("INSERT INTO recording VALUES (1, 1000.0)")
    conn.commit()
    conn.close()
    return tmp_path


def test_run_loop_does_not_exit_on_empty_queue(capture_dir):
    """Regression test: _run must NOT exit after 10s of empty queue.

    Previously, queue.Empty (normal timeout) was caught by `except Exception`,
    which incremented consecutive_errors. After 5 timeouts (10s), the thread
    exited — killing the processor before any chunk rotation message arrived.
    """
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir,
        q,
        ack_q,
        recording_name="test",
        upload_enabled=False,
        auto_delete=False,
    )
    cp.start()

    # Wait 12 seconds — previously, the thread would die at ~10s
    time.sleep(12)

    # Thread must still be alive
    assert cp._thread is not None
    assert cp._thread.is_alive(), (
        "ChunkProcessor thread died after 12s of empty queue — "
        "queue.Empty must not be treated as an error"
    )

    # Clean shutdown
    cp.stop(timeout=5)


def test_run_loop_processes_message_after_long_wait(capture_dir):
    """Verify ChunkProcessor processes a chunk message even after a long idle period."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir,
        q,
        ack_q,
        recording_name="test",
        upload_enabled=False,
        auto_delete=False,
    )
    cp.start()

    # Wait longer than old 10s death threshold
    time.sleep(12)

    # Now send a chunk message — processor should handle it
    # (We'll just check thread is alive and can receive the poison pill)
    assert cp._thread.is_alive()

    # Send poison pill to verify the thread is responsive
    q.put({"type": "poison_pill"})
    cp._thread.join(timeout=5)
    assert not cp._thread.is_alive(), "Thread should have exited after poison pill"


def test_all_chunks_uploaded_empty_returns_false(capture_dir):
    """all_chunks_uploaded() must return False when no chunks were processed."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir, q, ack_q, recording_name="test",
        upload_enabled=False, auto_delete=False,
    )
    assert cp.all_chunks_uploaded() is False
