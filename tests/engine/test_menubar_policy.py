"""Tests for the ``MenubarPolicy`` seam (SCR-41, slice 4b of SCR-31).

Promotes the ``_skip_menubar_spawn``-gated spawn block and the three IPC
queues from ``LegacyOptions`` to a pluggable policy. Two implementations:

* ``SpawnNewMenubar`` — standalone CLI: spawns the menubar subprocess,
  exposes ``proc`` / ``state_file`` for legacy callers, writes
  ``STATE_PROCESSING`` on transition, delegates kill to ``_kill_menubar``.
* ``Noop``            — session worker: all lifecycle hooks are no-ops;
  the ``SessionController`` owns the persistent menubar.

These tests pin the seam-level behavioural contract. The wiring into
``_run_screen_recorder`` is exercised by ``test_screen_recorder_parity``.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from unittest import mock


# ---------------------------------------------------------------------------
# Cycle 1 — smoke test
# ---------------------------------------------------------------------------


def test_menubar_policy_module_exposes_protocol_and_impls():
    """Smoke test: the module exists with the three documented names.

    Both ``SpawnNewMenubar`` and ``Noop`` must be constructible with no
    args — ``RecordingPolicies`` is built by the CLI adapter and the
    ``SessionController`` worker without any per-recording context at the
    construction site.
    """
    from screencap.engine.menubar_policy import MenubarPolicy, Noop, SpawnNewMenubar  # noqa: F401

    SpawnNewMenubar()
    Noop()


# ---------------------------------------------------------------------------
# Cycle 2 — Noop spawn is a no-op
# ---------------------------------------------------------------------------


def test_noop_spawn_does_not_start_a_subprocess(tmp_path):
    """``Noop.spawn`` must not launch any subprocess.

    Workers run inside the ``SessionController`` process tree; the
    controller owns the persistent menubar. If a worker spawned its own,
    there would be two menubar processes competing over the same IPC
    queues.
    """
    from screencap.engine.menubar_policy import Noop
    from screencap.engine.screen_recorder import IpcChannels

    channels = IpcChannels.create()
    with mock.patch("screencap.recorder._spawn_menubar") as spawn:
        Noop().spawn(
            "rec-1", 0.0, tmp_path, channels,
            audio_enabled=True, prompt_enabled=True,
        )

    spawn.assert_not_called()


# ---------------------------------------------------------------------------
# Cycle 3 — Noop lifecycle methods are safe no-ops
# ---------------------------------------------------------------------------


def test_noop_lifecycle_methods_are_safe_noops():
    """``Noop.notify_processing`` and ``Noop.kill`` must not raise and
    must not touch the filesystem or any subprocess."""
    from screencap.engine.menubar_policy import Noop

    policy = Noop()
    policy.notify_processing()
    policy.kill()

    assert policy.proc is None
    assert policy.state_file is None
    assert policy.owns_channels is False


# ---------------------------------------------------------------------------
# Cycle 4 — SpawnNewMenubar.spawn calls _spawn_menubar with channels
# ---------------------------------------------------------------------------


def test_spawn_new_menubar_spawn_calls_spawn_menubar_with_channels(tmp_path):
    """``SpawnNewMenubar.spawn`` must call ``_spawn_menubar`` and pass
    the three queues from ``IpcChannels`` as keyword args.

    Pins the delegation contract: the policy wraps ``_spawn_menubar``
    rather than re-implementing the subprocess wiring itself.
    """
    from screencap.engine.menubar_policy import SpawnNewMenubar
    from screencap.engine.screen_recorder import IpcChannels

    q_wf = multiprocessing.Queue()
    q_ov = multiprocessing.Queue()
    q_di = multiprocessing.Queue()
    channels = IpcChannels(window_feed=q_wf, override=q_ov, disable=q_di)

    fake_proc = mock.MagicMock()
    with mock.patch("screencap.recorder._spawn_menubar", return_value=fake_proc) as spawn:
        policy = SpawnNewMenubar()
        policy.spawn("rec-1", 1234.5, tmp_path, channels, audio_enabled=True, prompt_enabled=False)

    spawn.assert_called_once()
    _, kwargs = spawn.call_args
    assert kwargs["window_feed_q"] is q_wf
    assert kwargs["override_q"] is q_ov
    assert kwargs["disable_q"] is q_di
    assert kwargs["audio_enabled"] is True
    assert kwargs["prompt_enabled"] is False


# ---------------------------------------------------------------------------
# Cycle 5 — SpawnNewMenubar.spawn sets state_file inside capture_dir
# ---------------------------------------------------------------------------


def test_spawn_new_menubar_state_file_in_capture_dir(tmp_path):
    """``SpawnNewMenubar.spawn`` must set ``state_file`` to
    ``capture_dir / ".menubar_state"`` and expose the spawned process
    via the ``proc`` property.

    Both values are accessed by the legacy ``start_recording`` 4-tuple
    adapter and by ``DiskFullError``, so their locations are
    load-bearing.
    """
    from screencap.engine.menubar_policy import SpawnNewMenubar
    from screencap.engine.screen_recorder import IpcChannels

    fake_proc = mock.MagicMock()
    with mock.patch("screencap.recorder._spawn_menubar", return_value=fake_proc):
        policy = SpawnNewMenubar()
        policy.spawn("rec-1", 0.0, tmp_path, IpcChannels.create(), audio_enabled=False, prompt_enabled=True)

    assert policy.state_file == tmp_path / ".menubar_state"
    assert policy.proc is fake_proc


# ---------------------------------------------------------------------------
# Cycle 6 — SpawnNewMenubar.notify_processing writes STATE_PROCESSING
# ---------------------------------------------------------------------------


def test_spawn_new_menubar_notify_processing_writes_state_processing(tmp_path):
    """``SpawnNewMenubar.notify_processing`` must write ``STATE_PROCESSING``
    to the state file so the menubar UI transitions from the live-
    recording panel to the "processing" indicator.
    """
    from screencap.engine.menubar_policy import SpawnNewMenubar
    from screencap.engine.screen_recorder import IpcChannels

    fake_proc = mock.MagicMock()
    with mock.patch("screencap.recorder._spawn_menubar", return_value=fake_proc):
        policy = SpawnNewMenubar()
        policy.spawn("rec-1", 0.0, tmp_path, IpcChannels.create(), audio_enabled=True, prompt_enabled=True)

    policy.notify_processing()

    from screencap.menubar import STATE_PROCESSING
    assert policy.state_file.read_text() == STATE_PROCESSING


# ---------------------------------------------------------------------------
# Cycle 7 — SpawnNewMenubar.kill delegates to _kill_menubar
# ---------------------------------------------------------------------------


def test_spawn_new_menubar_kill_delegates_to_kill_menubar(tmp_path):
    """``SpawnNewMenubar.kill`` must call ``_kill_menubar`` with the
    spawned process and state file, which writes ``STATE_DONE`` then
    SIGKILLs.

    Using ``_kill_menubar`` rather than re-implementing the SIGKILL
    sequence directly keeps the signal-safe teardown logic in one place
    and makes ``SpawnNewMenubar`` the thin delegation layer it should be.
    """
    from screencap.engine.menubar_policy import SpawnNewMenubar
    from screencap.engine.screen_recorder import IpcChannels

    fake_proc = mock.MagicMock()
    with mock.patch("screencap.recorder._spawn_menubar", return_value=fake_proc):
        policy = SpawnNewMenubar()
        policy.spawn("rec-1", 0.0, tmp_path, IpcChannels.create(), audio_enabled=True, prompt_enabled=True)

    with mock.patch("screencap.recorder._kill_menubar") as kill:
        policy.kill()

    kill.assert_called_once_with(fake_proc, tmp_path / ".menubar_state")
