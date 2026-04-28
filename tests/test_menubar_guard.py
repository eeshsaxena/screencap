"""Tests for Unit 6: SCREENCAP_PARENT=swiftui env-var guard on _run_menubar.

When the SwiftUI app spawns ``screencap start`` it sets
``SCREENCAP_PARENT=swiftui`` so the Python rumps menu bar no-ops (the
SwiftUI shell owns the menu bar in v1). Any other env-var value (or unset)
keeps the legacy menu bar attached.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import time

import pytest

from screencap import menubar


def test_returns_immediately_when_swiftui_env_set(monkeypatch, capsys):
    """SCREENCAP_PARENT=swiftui → _run_menubar returns immediately, no AppKit init."""
    monkeypatch.setenv("SCREENCAP_PARENT", "swiftui")

    start = time.monotonic()
    menubar._run_menubar(
        parent_pid=os.getpid(),
        recording_name="probe",
        start_time=time.time(),
        state_file="",
    )
    elapsed = time.monotonic() - start

    # Sub-second exit means we never touched AppKit / NSApplication / rumps.
    # AppKit init alone takes hundreds of ms.
    assert elapsed < 0.5, f"guard should be fast, took {elapsed:.3f}s"

    # Guard emits a structured stderr warning so misconfiguration is observable.
    captured = capsys.readouterr()
    payloads = [
        json.loads(line)
        for line in captured.err.strip().splitlines()
        if line.startswith("{")
    ]
    assert any(p.get("type") == "menubar_neutralized_by_env" for p in payloads)


def test_does_not_return_immediately_when_env_unset(monkeypatch):
    """Without SCREENCAP_PARENT=swiftui, the guard does NOT fire — function
    proceeds into AppKit init (which we stub out so the test stays headless)."""
    monkeypatch.delenv("SCREENCAP_PARENT", raising=False)

    # Stub AppKit so the test doesn't actually open NSApplication. We use a
    # mock that raises a sentinel exception the moment the function moves
    # past the guard — proving the guard didn't no-op.
    import sys

    sentinel = RuntimeError("guard did not fire — moved past env check")

    class _StubAppKit:
        def __getattr__(self, name):
            raise sentinel

    monkeypatch.setitem(sys.modules, "AppKit", _StubAppKit())

    with pytest.raises(RuntimeError, match="guard did not fire"):
        menubar._run_menubar(
            parent_pid=os.getpid(),
            recording_name="probe",
            start_time=time.time(),
            state_file="",
        )


@pytest.mark.parametrize("value", ["cli", "", "SwiftUI", "swiftui-2"])
def test_strict_equality_only_matches_lowercase_swiftui(value, monkeypatch):
    """Guard fires only on the exact string \"swiftui\" — every other value
    proceeds into the legacy menu bar path."""
    monkeypatch.setenv("SCREENCAP_PARENT", value)

    import sys

    sentinel = RuntimeError("non-swiftui value should NOT trigger guard")

    class _StubAppKit:
        def __getattr__(self, name):
            raise sentinel

    monkeypatch.setitem(sys.modules, "AppKit", _StubAppKit())

    with pytest.raises(RuntimeError, match="non-swiftui value"):
        menubar._run_menubar(
            parent_pid=os.getpid(),
            recording_name="probe",
            start_time=time.time(),
            state_file="",
        )


# ---------------------------------------------------------------------------
# Cross-process env propagation (multiprocessing.spawn inherits env)
# ---------------------------------------------------------------------------


def _child_check_env(result_q):
    """Subprocess target: report the inherited SCREENCAP_PARENT value."""
    result_q.put(os.environ.get("SCREENCAP_PARENT", "<unset>"))


def test_env_propagates_to_spawn_workers(monkeypatch):
    """multiprocessing.spawn workers inherit the parent's env — the guard
    will fire in spawn workers too. Pinning this prevents a regression
    where someone changes process spawning to wipe env."""
    monkeypatch.setenv("SCREENCAP_PARENT", "swiftui")

    ctx = multiprocessing.get_context("spawn")
    result_q = ctx.Queue()
    proc = ctx.Process(target=_child_check_env, args=(result_q,))
    proc.start()
    try:
        value = result_q.get(timeout=10)
        assert value == "swiftui"
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
