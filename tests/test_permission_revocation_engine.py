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

    def test_pyobjc_bridge_error_does_not_crash_recorder(self, monkeypatch):
        """Todo 013: PyObjC bridge errors (objc.error, runtime errors during
        Sequoia overlays / system update prompts) propagated uncaught into
        the recording hot loop. Now wrapped in try/except — broker errors
        are treated as 'permission still valid for this tick' so the watcher
        retries on the next interval instead of killing the recorder."""
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")

        def _broken(name):
            raise RuntimeError("simulated PyObjC bridge failure")

        monkeypatch.setattr(recorder, "_check_permission_fresh", _broken)

        # Must not raise — and must report "all ok" because the bridge
        # failure can't be distinguished from a real revocation, so we
        # fail open for this tick.
        ok, missing = recorder._check_permissions_now()
        assert ok is True
        assert missing is None

    def test_missing_darwin_module_returns_ok(self, monkeypatch):
        """If the darwin module isn't importable, fail-open (don't kill the
        recorder for an unrelated import error)."""
        from screencap import recorder

        monkeypatch.setattr(sys, "platform", "darwin")

        # Force the import to fail
        original_module = sys.modules.pop("screencap.engine.platform.darwin", None)
        try:
            class _BrokenLoader:
                @classmethod
                def __getitem__(cls, key):
                    if key == "screencap.engine.platform.darwin":
                        raise ImportError("simulated")
                    raise KeyError(key)

            with mock.patch.dict(
                sys.modules,
                {"screencap.engine.platform.darwin": None},
                clear=False,
            ):
                # Force ImportError when the function tries to import
                with mock.patch(
                    "builtins.__import__",
                    side_effect=lambda name, *a, **kw: (
                        (_ for _ in ()).throw(ImportError("simulated"))
                        if "darwin" in name
                        else __import__(name, *a, **kw)
                    ),
                ):
                    ok, missing = recorder._check_permissions_now()
            assert ok is True
            assert missing is None
        finally:
            if original_module is not None:
                sys.modules["screencap.engine.platform.darwin"] = original_module


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
