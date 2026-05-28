"""Daemon-spawn engine-worker Screen Recording preflight.

The daemon spawns ``run_recording_worker`` with ``_permission_policy=PermNoop``
to avoid the standalone-CLI's console prompt path. Without a startup
preflight, an unauthorized daemon binary (e.g., a fresh PyInstaller build
whose code-signing identifier doesn't match the user's prior TCC grant)
enters ``screen_event_reader`` at 20 fps and triggers a TCC consent prompt
on every ``screencapture`` / ``CGWindowListCopyWindowInfo`` call — the
infinite-prompt-loop symptom users see.

These tests pin the contract: on macOS, ``run_recording_worker`` MUST
fail-fast with ``permission_lost`` + exit code 3 when ``Quartz``'s live
TCC state reports Screen Recording is denied, without ever entering the
recording loop. PyObjC import or call failures fail-open — a transient
Quartz hiccup must not kill an otherwise-authorized run.

The preflight uses an in-process ``Quartz.CGPreflightScreenCaptureAccess``
call rather than the subprocess probe ``_check_permission_fresh`` because
the bundled daemon binary's Click entry point rejects the probe's ``-c``
flag with a UsageError, making the subprocess probe return ``None`` (which
fail-opens) and the preflight a no-op in the frozen binary path.
"""

from __future__ import annotations

import json
import sys
import types
from io import StringIO

import pytest


def _install_fake_quartz(monkeypatch, *, granted):
    """Inject a fake ``Quartz`` module so the worker's in-process preflight
    check returns a known answer without needing PyObjC on the test host.

    ``granted`` is ``True`` (allowed), ``False`` (denied), or an exception
    instance to raise from ``CGPreflightScreenCaptureAccess`` to simulate a
    PyObjC failure (the fail-open path).
    """
    fake = types.ModuleType("Quartz")
    if isinstance(granted, BaseException):
        def _raise():  # noqa: D401
            raise granted
        fake.CGPreflightScreenCaptureAccess = _raise
    else:
        fake.CGPreflightScreenCaptureAccess = lambda: 1 if granted else 0
    monkeypatch.setitem(sys.modules, "Quartz", fake)


@pytest.fixture
def minimal_worker_args(tmp_path):
    """Smallest valid args dict for run_recording_worker.

    The preflight gate runs *before* start_recording, so the args only need
    to be structurally valid — none of them are dereferenced on the deny
    path. capture_dir_hint must point to a writable location for the
    error-log path the worker walks on uncaught exceptions.
    """
    import multiprocessing

    return {
        "name": "preflight-test",
        "capture_dir_hint": str(tmp_path),
        "_window_feed_q": multiprocessing.Queue(),
        "_override_q": multiprocessing.Queue(),
        "_disable_q": multiprocessing.Queue(),
        "_network_handoff_ready": None,
    }


def _capture_stderr(callable_):
    buf = StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        callable_()
    finally:
        sys.stderr = saved
    return buf.getvalue()


class TestDaemonPreflight:
    def test_darwin_denied_emits_permission_lost_and_exits_3(
        self, monkeypatch, minimal_worker_args
    ):
        """Live-TCC denied → emit permission_lost, raise SystemExit(3).

        Pinned together because the SwiftUI shell's exit-code fast path
        relies on BOTH signals: the stderr event drives the in-recording
        UX, and the exit code is the authoritative final-state hint that
        cli.py's exit handler propagates.
        """
        from screencap import recorder, session

        monkeypatch.setattr(sys, "platform", "darwin")
        _install_fake_quartz(monkeypatch, granted=False)

        # start_recording MUST NOT be called when preflight denies — that
        # would re-introduce the 20 fps prompt loop the gate exists to
        # prevent. Raise from the mock so an accidental call is loud.
        called = {"count": 0}

        def _should_not_run(*args, **kwargs):
            called["count"] += 1
            raise AssertionError("start_recording invoked on denied preflight")

        monkeypatch.setattr(recorder, "start_recording", _should_not_run)

        # Capture stderr around the call WITHOUT pytest.raises (so the
        # SystemExit propagates out of `_capture_stderr`'s finally block
        # and stays catchable here). The try/except mirrors what the
        # daemon supervisor sees in production: a SystemExit(3) raises
        # out of the worker subprocess, leaving the stderr event behind.
        buf = StringIO()
        saved = sys.stderr
        sys.stderr = buf
        try:
            session.run_recording_worker(minimal_worker_args)
            pytest.fail("expected SystemExit(3) on TCC denial")
        except SystemExit as exc:
            assert exc.code == 3
        finally:
            sys.stderr = saved

        assert called["count"] == 0

        # Verify the permission_lost event went to stderr in the documented
        # JSON contract. SwiftUI's RecorderController parses these lines.
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]
        permission_events = [e for e in events if e.get("type") == "permission_lost"]
        assert len(permission_events) == 1
        assert permission_events[0]["permission"] == "screen_recording"

    def test_non_darwin_skips_preflight(self, monkeypatch, minimal_worker_args):
        """Linux / Windows: TCC doesn't exist — preflight must no-op so
        start_recording runs. Without this, the daemon would refuse to
        start on every non-macOS host the integration test suite covers."""
        from screencap import recorder, session

        monkeypatch.setattr(sys, "platform", "linux")
        # If the preflight is reached on Linux it would attempt the
        # Quartz import and explode, so a Quartz that raises on call is
        # the canary: it must never be touched on this code path.
        _install_fake_quartz(
            monkeypatch, granted=AssertionError("Quartz called on non-darwin")
        )

        called = {"count": 0}

        def _fake_start(*args, **kwargs):
            called["count"] += 1
            raise SystemExit(0)  # stop early; we only care that we reached it

        monkeypatch.setattr(recorder, "start_recording", _fake_start)

        with pytest.raises(SystemExit) as exc_info:
            session.run_recording_worker(minimal_worker_args)

        assert exc_info.value.code == 0
        assert called["count"] == 1

    def test_darwin_quartz_failure_fails_open(self, monkeypatch, minimal_worker_args):
        """``Quartz.CGPreflightScreenCaptureAccess`` can raise on a
        PyObjC hiccup. The preflight must treat that as 'couldn't
        determine' and continue into start_recording — otherwise a
        transient Quartz failure during a Sequoia overlay would kill
        every recording on machines where TCC is actually granted."""
        from screencap import recorder, session

        monkeypatch.setattr(sys, "platform", "darwin")
        _install_fake_quartz(monkeypatch, granted=RuntimeError("PyObjC blew up"))

        called = {"count": 0}

        def _fake_start(*args, **kwargs):
            called["count"] += 1
            raise SystemExit(0)

        monkeypatch.setattr(recorder, "start_recording", _fake_start)

        with pytest.raises(SystemExit) as exc_info:
            session.run_recording_worker(minimal_worker_args)

        assert exc_info.value.code == 0
        assert called["count"] == 1

    def test_darwin_denial_via_inprocess_check_when_subprocess_probe_broken(
        self, monkeypatch, minimal_worker_args
    ):
        """Frozen-daemon regression: the bundled CLI's Click entry point
        rejects ``screencap -c "<code>"`` with a UsageError, so the
        subprocess-based ``_check_permission_fresh`` always returns
        ``None`` in production. The preflight must still deny based on
        the in-process ``Quartz`` check, not on the broken probe.
        """
        from screencap import recorder, session

        monkeypatch.setattr(sys, "platform", "darwin")
        _install_fake_quartz(monkeypatch, granted=False)
        # Pin the subprocess-probe regression: even if some refactor
        # accidentally re-introduces the call, returning None must NOT
        # let the worker fall through into the recording loop.
        monkeypatch.setattr(recorder, "_check_permission_fresh", lambda name: None)

        def _should_not_run(*args, **kwargs):
            raise AssertionError("start_recording invoked on denied preflight")

        monkeypatch.setattr(recorder, "start_recording", _should_not_run)

        with pytest.raises(SystemExit) as exc_info:
            session.run_recording_worker(minimal_worker_args)
        assert exc_info.value.code == 3

    def test_darwin_granted_proceeds_to_start_recording(
        self, monkeypatch, minimal_worker_args
    ):
        """Live-TCC granted → preflight passes silently, no permission_lost
        event, start_recording runs. The happy path stays unchanged."""
        from screencap import recorder, session

        monkeypatch.setattr(sys, "platform", "darwin")
        _install_fake_quartz(monkeypatch, granted=True)

        called = {"count": 0}

        def _fake_start(*args, **kwargs):
            called["count"] += 1
            raise SystemExit(0)

        monkeypatch.setattr(recorder, "start_recording", _fake_start)

        # Capture stderr around the call so we can assert no
        # permission_lost event leaked from the granted-preflight branch.
        buf = StringIO()
        saved = sys.stderr
        sys.stderr = buf
        try:
            session.run_recording_worker(minimal_worker_args)
            pytest.fail("expected SystemExit(0) from mocked start_recording")
        except SystemExit as exc:
            assert exc.code == 0
        finally:
            sys.stderr = saved

        assert "permission_lost" not in buf.getvalue()
        assert called["count"] == 1
