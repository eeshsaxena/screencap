"""Tests for Unit 8: mid-recording permission revocation handling.

Engine-side detection: the recorder's main loop polls
``_check_permissions_now()`` every 3-5s. On revocation, the recorder emits
a ``permission_lost`` stderr event (Unit 8a contract) and triggers graceful
shutdown via ``recorder.stop()``.

These tests cover the helper directly (cross-platform safe). The full
integration of the watcher with the recording loop is implicitly tested
by the fact that the watcher uses the existing _stop_event signaling path
that the rest of the recorder already exercises.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from unittest import mock

import pytest


def _capture_stderr(callable_):
    buf = StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        callable_()
    finally:
        sys.stderr = saved
    return buf.getvalue()


class TestCheckPermissionsNow:
    """Pin the contract of ``_check_permissions_now()`` — the polling primitive."""

    def test_non_darwin_returns_ok(self, monkeypatch):
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "linux")
        ok, missing = recorder._check_permissions_now()
        assert ok is True
        assert missing is None

    def test_all_granted_returns_ok(self, monkeypatch):
        """Post-todo-002: watcher uses _check_permission_fresh (subprocess);
        mock that helper directly instead of the in-process DarwinPlatform."""
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(recorder, "_check_permission_fresh", lambda name: True)

        ok, missing = recorder._check_permissions_now()
        assert ok is True
        assert missing is None

    @pytest.mark.parametrize("revoked,expected_name", [
        ("Screen Recording", "screen_recording"),
        ("Accessibility", "accessibility"),
        ("Input Monitoring", "input_monitoring"),
    ])
    def test_revoked_permission_reported(self, monkeypatch, revoked, expected_name):
        """Post-todo-002: revoke a single TCC permission via the fresh-subprocess
        helper and assert the watcher reports it."""
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")

        def _fresh(name: str) -> bool:
            return name != revoked

        monkeypatch.setattr(recorder, "_check_permission_fresh", _fresh)

        ok, missing = recorder._check_permissions_now()
        assert ok is False
        assert missing == expected_name

    def test_screen_recording_takes_priority(self, monkeypatch):
        """When multiple are revoked, screen_recording is reported first
        (it's the most user-impactful loss)."""
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(recorder, "_check_permission_fresh", lambda name: False)

        ok, missing = recorder._check_permissions_now()
        assert ok is False
        assert missing == "screen_recording"

    def test_probe_failure_does_not_trigger_revocation(self, monkeypatch):
        """Tri-state contract: _check_permission_fresh returns None when the
        subprocess probe fails (timeout, OSError, unparseable stdout). The
        watcher must NOT treat None as a revocation — that would kill the
        recording on a transient Quartz hiccup. Previously a bool-only
        contract returned False on probe failure → permission_lost emitted →
        recording aborted.
        """
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(recorder, "_check_permission_fresh", lambda name: None)

        ok, missing = recorder._check_permissions_now()
        assert ok is True
        assert missing is None

    def test_pyobjc_bridge_error_does_not_crash_recorder(self, monkeypatch):
        """Defensive belt-and-suspenders: even if _check_permission_fresh
        raises (a future regression that breaks the documented tri-state
        contract), the watcher's outer try/except catches it and treats
        the tick as 'couldn't determine' rather than killing the recorder."""
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")

        def _broken(name):
            raise RuntimeError("simulated PyObjC bridge failure")

        monkeypatch.setattr(recorder, "_check_permission_fresh", _broken)

        ok, missing = recorder._check_permissions_now()
        assert ok is True
        assert missing is None

    def test_explicit_false_still_triggers_revocation(self, monkeypatch):
        """Tri-state's other half: a clean False (probe succeeded, permission
        denied) MUST still trigger revocation. Otherwise the fix would mask
        real revocations as 'couldn't determine'."""
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")

        def _denied(name):
            # Screen Recording is denied; everything else granted.
            return False if name == "Screen Recording" else True

        monkeypatch.setattr(recorder, "_check_permission_fresh", _denied)

        ok, missing = recorder._check_permissions_now()
        assert ok is False
        assert missing == "screen_recording"

    def test_check_permission_fresh_returns_none_on_subprocess_timeout(self, monkeypatch):
        """Direct check on the helper: subprocess timeout → None (not False).
        Pin the contract — the previous bool-only return was the root cause
        of the false-revocation bug."""
        import subprocess as _subprocess
        from screencap import recorder

        def _timeout(*args, **kwargs):
            raise _subprocess.TimeoutExpired(cmd=args[0], timeout=5)

        monkeypatch.setattr(_subprocess, "run", _timeout)
        result = recorder._check_permission_fresh("Screen Recording")
        assert result is None, f"timeout should return None, got {result!r}"

    def test_check_permission_fresh_returns_none_on_unparseable_stdout(self, monkeypatch):
        """Defensive: if the subprocess succeeds but emits something other
        than "True"/"False" (Python startup error, garbage), treat as
        couldn't-determine rather than denied."""
        import subprocess as _subprocess
        from screencap import recorder

        monkeypatch.setattr(
            _subprocess,
            "run",
            lambda *a, **k: _subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ImportError: traceback...\n", stderr=""
            ),
        )
        result = recorder._check_permission_fresh("Screen Recording")
        assert result is None

    def test_missing_darwin_module_returns_ok(self, monkeypatch):
        """If the probe subprocess can't import the darwin module, fail-open
        (don't kill the recorder for an unrelated import error).

        The probe runs in a fresh subprocess (``_check_permission_fresh``), so
        a missing/broken darwin import surfaces as an ImportError traceback on
        the subprocess's stdout rather than the expected "True"/"False". That
        unparseable output is treated as "couldn't determine" (None), and
        ``_check_permissions_now`` fails open with (True, None) — the
        ``builtins.__import__`` layer in the parent process is irrelevant here.
        """
        import subprocess as _subprocess
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")

        monkeypatch.setattr(
            _subprocess,
            "run",
            lambda *a, **k: _subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "Traceback (most recent call last):\n"
                    "ModuleNotFoundError: No module named "
                    "'screencap.engine.platform.darwin'\n"
                ),
                stderr="",
            ),
        )

        ok, missing = recorder._check_permissions_now()
        assert ok is True
        assert missing is None


class TestPermissionLostEventEmission:
    """Verify the stderr event payload matches Unit 8a's contract when the
    watcher detects revocation."""

    def test_emit_event_payload_shape(self):
        """Direct verification that _emit_event produces the documented schema."""
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event(
            "permission_lost",
            permission="screen_recording",
            elapsed=42.5,
        ))
        line = next(
            line for line in out.strip().splitlines()
            if line.startswith("{")
        )
        evt = json.loads(line)
        assert evt["type"] == "permission_lost"
        assert evt["permission"] == "screen_recording"
        assert evt["elapsed"] == 42.5
        assert "ts" in evt
