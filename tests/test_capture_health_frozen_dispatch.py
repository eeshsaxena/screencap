"""SCR-76 R12 — the "green-in-tests, broken-in-frozen" regression class.

SCR-69 slipped past CI because the mid-recording watcher probed TCC by spawning
``[sys.executable, "-c", code]`` — a no-op in the frozen daemon binary (the Click
entry point rejects ``-c``) that returned ``None`` and fail-open did nothing.
The tests that "covered" it mocked ``_check_permission_fresh`` directly and never
exercised the subprocess machinery, so the frozen-mode breakage was invisible.

These tests pin that the replacement capture-side mechanism CANNOT recur that
class:

1. The full detection → attribution → emission WIRING (``_capture_health_tick``)
   is exercised end-to-end with stubs — including warmup gating, alive-filtering,
   and the TCC-vs-advisory branch — without spawning the engine. This is the
   path that runs identically under the frozen ``_engine-worker`` entry, because
   it uses only ``emit_event`` (stderr, frozen-safe) and in-process Quartz.
2. Detection is structurally and behaviourally INDEPENDENT of the broken
   subprocess probe: it works with ``sys.frozen=True`` and the old probe forced
   to ``None``, and references neither ``subprocess`` nor ``_check_permission_fresh``.

Origin plan: docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
"""

from __future__ import annotations

import sys

from screencap.engine import recorder
from tests._capture_health_helpers import _ALIVE, _capture_stderr, _referenced_names


def _run_ticks(tick_inputs, *, window_secs=10.0, debounce=3, probe=lambda r: None):
    """Drive _capture_health_tick across a sequence of (cur_counts, elapsed,
    action_alive) inputs against a stub emit, returning the emitted event
    payloads in order. Mirrors how record()'s supervisor calls the tick."""
    runs, emitted = {}, {}
    prev = None
    captured: list[dict] = []

    def emit(et, **fields):
        captured.append({"type": et, **fields})

    for cur, elapsed, action_alive in tick_inputs:
        recorder._capture_health_tick(
            prev_counts=prev,
            cur_counts=cur,
            elapsed=elapsed,
            window_secs=window_secs,
            debounce=debounce,
            runs=runs,
            emitted=emitted,
            alive=_ALIVE,
            action_alive=action_alive,
            emit=emit,
            probe=probe,
        )
        prev = cur
    return captured


# ---------------------------------------------------------------------------
# Full detection → attribution → emission wiring (the frozen-safe path)
# ---------------------------------------------------------------------------


def test_warmup_window_suppresses_verdict_then_fires_after():
    # Before one full window has elapsed, no verdict even with a clear gap.
    # screen attempts climb (+20/tick), output flat → unhealthy once warmed.
    ticks = []
    attempt = 0
    for i in range(1, 8):
        attempt += 20
        # elapsed crosses window_secs=10 at tick 6 (i*2 seconds)
        ticks.append(({"screen.attempt": attempt, "screen.output": 0,
                       "window.attempt": 2 * i, "window.output": 2 * i},
                      i * 2.0, True))
    captured = _run_ticks(ticks, window_secs=10.0, debounce=3)
    # Warmup ends at elapsed>=10 (tick i=5 → elapsed 10.0). Debounce 3 ticks
    # after that → fires once, as capture_unhealthy (probe stub → inconclusive).
    assert len(captured) == 1
    assert captured[0]["type"] == "capture_unhealthy"
    assert captured[0]["reader"] == "screen"
    assert captured[0]["reason"] == "reader_stalled"


def test_tcc_attribution_routes_to_permission_lost():
    # Screen stalled AND the labeller attributes Screen Recording denial →
    # the wiring emits the terminal-capable permission_lost (AE1).
    attempt = 0
    ticks = []
    for i in range(1, 5):
        attempt += 20
        ticks.append(({"screen.attempt": attempt, "screen.output": 0}, 100.0 + i, True))
    captured = _run_ticks(ticks, window_secs=0.0, debounce=3,
                          probe=lambda r: "screen_recording")
    assert len(captured) == 1
    assert captured[0]["type"] == "permission_lost"
    assert captured[0]["permission"] == "screen_recording"
    assert isinstance(captured[0]["elapsed"], float)


def test_action_listener_dead_routes_to_capture_unhealthy_listener_dead():
    # AE2: action listener dead (heartbeat False), no TCC attribution → advisory.
    ticks = [({}, 100.0 + i, False) for i in range(1, 5)]
    captured = _run_ticks(ticks, window_secs=0.0, debounce=3)
    assert len(captured) == 1
    assert captured[0]["type"] == "capture_unhealthy"
    assert captured[0]["reader"] == "action"
    assert captured[0]["reason"] == "listener_dead"


def test_labeller_is_failopen_and_edge_still_surfaces():
    # AE4: the in-process labeller is fail-open — any internal failure yields
    # "inconclusive" rather than raising, so an unhealthy edge still surfaces
    # (advisory) and the recording continues. Drive the tick with the REAL
    # _probe_tcc_denied to pin that it never raises and exactly one event fires.
    attempt = 0
    ticks = []
    for i in range(1, 5):
        attempt += 20
        ticks.append(({"screen.attempt": attempt, "screen.output": 0}, 100.0 + i, True))
    captured = _run_ticks(ticks, window_secs=0.0, debounce=3,
                          probe=recorder._probe_tcc_denied)
    assert len(captured) == 1  # exactly one edge emitted, no crash
    # Real probe → permission_lost if a permission is actually denied on this
    # host, else the advisory capture_unhealthy. Either is a valid surfaced edge.
    assert captured[0]["type"] in ("capture_unhealthy", "permission_lost")


def test_real_emit_through_tick_produces_one_stderr_line():
    # End-to-end through the REAL emit_event (stderr) — the same channel the
    # frozen daemon bridges to the shell.
    from screencap._stderr_events import emit_event

    runs, emitted = {}, {}

    def drive():
        prev = {"screen.attempt": 0, "screen.output": 0}
        for i in range(1, 4):
            cur = {"screen.attempt": 20 * i, "screen.output": 0}
            recorder._capture_health_tick(
                prev_counts=prev, cur_counts=cur, elapsed=100.0 + i,
                window_secs=0.0, debounce=3, runs=runs, emitted=emitted,
                alive=_ALIVE, action_alive=True, emit=emit_event,
                probe=lambda r: None,
            )
            prev = cur

    evts = _capture_stderr(drive)
    assert len(evts) == 1
    assert evts[0]["type"] == "capture_unhealthy"
    assert evts[0]["reader"] == "screen"
    assert "schema_version" in evts[0]


# ---------------------------------------------------------------------------
# Independence from the frozen-broken subprocess probe
# ---------------------------------------------------------------------------


def test_detection_works_with_frozen_and_broken_probe(monkeypatch):
    """Even with sys.frozen=True and the old _check_permission_fresh forced to
    None (its exact broken-in-frozen behaviour), the capture-side wiring still
    detects the attempt-vs-output gap and emits."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    import screencap.recorder as cli_recorder
    monkeypatch.setattr(cli_recorder, "_check_permission_fresh", lambda _n: None)

    attempt = 0
    ticks = []
    for i in range(1, 5):
        attempt += 20
        ticks.append(({"screen.attempt": attempt, "screen.output": 0}, 100.0 + i, True))
    captured = _run_ticks(ticks, window_secs=0.0, debounce=3)
    assert len(captured) == 1
    assert captured[0]["type"] == "capture_unhealthy"


def test_capture_health_surface_never_references_broken_probe_or_subprocess():
    """Structural guard (mirrors test_health_monitoring's AST checks): the
    capture-health code path must never import subprocess or call the
    frozen-broken _check_permission_fresh."""
    for fn in (
        recorder._probe_tcc_denied,
        recorder._action_listener_alive,
        recorder._capture_health_step,
        recorder._emit_capture_health_event,
        recorder._capture_health_tick,
    ):
        names = _referenced_names(fn)
        assert "subprocess" not in names, f"{fn.__name__} must not use subprocess"
        assert "_check_permission_fresh" not in names, (
            f"{fn.__name__} must not call the frozen-broken probe"
        )
