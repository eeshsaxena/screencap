"""SIGINT-during-setup integration test (SCR-39, slice 3 of SCR-31).

Locks down the most fragile invariant of the recording lifecycle:
**signal handlers must be installed BEFORE ``Recorder.__enter__()``
completes, and the SIGINT handler must tolerate ``recorder=None``** —
otherwise a Ctrl+C during the setup window between handler-install and
the engine's ``__enter__`` returning would crash with ``AttributeError``
on ``recorder.stop()``.

Until SCR-39 this invariant lived only in the recorder.py source comment
and a pair of AST guards in ``test_recorder.py::TestForceExitContracts``.
SCR-36 explicitly flagged the gap as Tier-3. This test fills it with
executable proof: a real ``ScreenRecorder`` runs in a subprocess; the
parent races a SIGINT into the gap; the subprocess must exit cleanly,
no ``AttributeError``.

Why a subprocess and not in-process: SIGINT in pytest's main process
disrupts the runner. The subprocess isolates the signal-handler global
state (``signal.signal`` mutates per-process state, not per-thread).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


# Path that the test passes to the driver — the driver writes a marker
# file the moment its slow ``Recorder.__enter__`` begins, so the parent
# can target the SIGINT precisely into the setup window.
_DRIVER = Path(__file__).parent / "_signal_during_setup_driver.py"


pytestmark = pytest.mark.timeout(30)


def test_sigint_during_recorder_setup_shuts_down_cleanly(tmp_path):
    """A SIGINT delivered while ``Recorder.__enter__()`` is mid-setup must:

    1. Be honoured (handler installed before ``__enter__`` was called).
    2. Not crash with ``AttributeError`` on ``recorder.stop()`` because
       the local ``recorder`` is still bound to ``None`` until
       ``__enter__`` returns.
    3. Result in a clean process exit (return code 0) once setup
       completes — the loop sees ``_stop_event`` already set and exits
       immediately.

    The driver subprocess uses a ``SlowFakeRecorder`` whose ``__enter__``
    writes a ``ready`` marker file and then sleeps for several seconds.
    The parent test waits for the marker, then ``os.kill(SIGINT)`` into
    the sleep — exactly the window the invariant protects.
    """
    rec_dir = tmp_path / "rec"
    ready_marker = tmp_path / "setup_started"
    setup_hold_seconds = 3.0

    proc = subprocess.Popen(
        [
            sys.executable,
            str(_DRIVER),
            "--output-dir", str(rec_dir),
            "--ready-marker", str(ready_marker),
            "--setup-hold", str(setup_hold_seconds),
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the driver to enter the setup window.
    deadline = time.time() + 10.0
    while not ready_marker.exists():
        if time.time() > deadline:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            pytest.fail(
                "Driver never wrote the ready marker — never reached "
                "the setup window.\n"
                f"stdout:\n{stdout.decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}"
            )
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=5)
            pytest.fail(
                f"Driver exited early with code {proc.returncode} before "
                "reaching the setup window.\n"
                f"stdout:\n{stdout.decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}"
            )
        time.sleep(0.05)

    # Now we're inside the (handler-installed, recorder=None) window.
    os.kill(proc.pid, signal.SIGINT)

    try:
        stdout, stderr = proc.communicate(timeout=15.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        pytest.fail(
            "Driver did not shut down within 15s of SIGINT.\n"
            f"stdout:\n{stdout.decode(errors='replace')}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )

    stdout_text = stdout.decode(errors="replace")
    stderr_text = stderr.decode(errors="replace")

    assert "AttributeError" not in stderr_text, (
        "SIGINT during setup raised AttributeError — the ``if recorder "
        "is not None`` guard regressed.\n"
        f"stderr:\n{stderr_text}"
    )

    assert proc.returncode == 0, (
        f"Driver exited non-zero ({proc.returncode}) — setup-window SIGINT "
        "did not produce a clean shutdown.\n"
        f"stdout:\n{stdout_text}\n"
        f"stderr:\n{stderr_text}"
    )

    # Capture dir was created — proves the wrapper got past preflight
    # before the SIGINT arrived (the SIGINT didn't short-circuit setup
    # before the recording dir was even materialised).
    assert rec_dir.exists(), (
        "Capture dir was never created — SIGINT may have aborted "
        "setup too early.\n"
        f"stdout:\n{stdout_text}\nstderr:\n{stderr_text}"
    )
