"""SCR-104 — real ``_engine-worker`` dispatch integration test for capture-health (R12).

Follow-up from SCR-76. The sibling fast tests in
``tests/test_capture_health_frozen_dispatch.py`` drive ``_capture_health_tick``
directly and add AST guards, but never spawn the real ``_engine-worker`` Click
entry point nor run ``record()``'s supervisor loop across the dispatch boundary.
That leaves the "green-in-tests, broken-in-frozen" class R12 was written to close
partly open: a wiring regression in ``record()``'s supervisor block — a key typo
in the ``_hc_alive`` dict, a dropped ``_health_prev_counts`` update, or deleting
the health-tick call — would pass every fast test yet be broken when the daemon
actually spawns the engine.

This test closes that gap end-to-end. It spawns the *real* ``python -m screencap
_engine-worker`` (the same hidden Click entry the frozen daemon dispatches to,
``src/screencap/cli/__init__.py``), forces the screen reader to stall, and
asserts the supervisor loop emits a ``capture_unhealthy`` stderr event. If the
health wiring in ``record()`` breaks, no event is emitted and the test fails on
timeout — even though the helper unit tests still pass. That is exactly R12's
verification criterion.

Marked ``slow`` (spawns a real, minimal recording; the verdict lands ~5-7 s in)
and macOS-only (capture-health is darwin-gated in ``record()``).
"""

from __future__ import annotations

import base64
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        sys.platform != "darwin",
        reason="capture-health detection is darwin-only in record()",
    ),
]

# Wall-clock budget for the spawned engine to emit a capture-health event.
# Env-tunable so slow / loaded hosts (or a lengthened warmup) can extend it.
_DISPATCH_DEADLINE_SECS = float(
    os.environ.get("SCREENCAP_TEST_DISPATCH_DEADLINE_SECS", "30.0")
)

# A ``sitecustomize.py`` injected on the engine subprocess's PYTHONPATH. It runs
# at interpreter startup (before ``python -m screencap`` imports anything), which
# is the only seam that lets us install the stubs the supervisor loop needs to
# observe a stalled screen reader without a real screen capture:
#
#   * take_screenshot — synthetic frames for a short warmup, then ``None``. The
#     warmup matters: the screen reader's startup barrier only signals "started"
#     AFTER a non-None frame, so an always-None stub would hang record() forever
#     at that barrier and never reach the supervisor loop. After warmup, ``None``
#     makes ``screen.attempt`` climb while ``screen.output`` stays flat — the
#     exact ``reader_stalled`` edge.
#   * get_active_window_data — falsy (per the issue's suggested direction).
#   * CGPreflightScreenCaptureAccess — forced True so run_recording_worker's
#     fail-fast preflight cannot short-circuit to ``permission_lost`` on a host
#     lacking Screen Recording permission. That keeps the supervisor loop the
#     ONLY path to a health event, so a wiring regression cannot be masked by a
#     preflight ``permission_lost``. (try/except: if PyObjC rejects the
#     assignment, it falls back to the host's real preflight.)
_SITECUSTOMIZE = '''
import os as _os
import sys as _sys
import time as _time

# Warmup window during which the stub returns a synthetic frame (so the screen
# reader's startup barrier passes), then stalls (returns None) to drive the
# ``reader_stalled`` edge. Env-tunable so slow hosts can lengthen it.
_WARMUP_SECS = float(_os.environ.get("SCREENCAP_TEST_WARMUP_SECS", "3.0"))
_t0 = [None]


def _make_stub():
    def _stub(*a, **k):
        now = _time.perf_counter()
        if _t0[0] is None:
            _t0[0] = now
        if now - _t0[0] < _WARMUP_SECS:
            try:
                from PIL import Image
            except Exception as _e:
                _sys.stderr.write(
                    "[sitecustomize] take_screenshot stub warmup frame FAILED "
                    "(PIL unavailable): %r\\n" % (_e,)
                )
                _sys.stderr.flush()
                return None
            return Image.new("RGB", (64, 64), (0, 0, 0))
        _time.sleep(0.01)  # throttle the post-warmup None loop
        return None
    return _stub


try:
    from screencap.engine import utils as _utils
    _utils.take_screenshot = _make_stub()
except Exception as _e:
    _sys.stderr.write(
        "[sitecustomize] take_screenshot stub install FAILED: %r\\n" % (_e,)
    )
    _sys.stderr.flush()

try:
    from screencap.engine import window as _window
    _window.get_active_window_data = lambda *a, **k: None
except Exception as _e:
    _sys.stderr.write(
        "[sitecustomize] get_active_window_data stub install FAILED: %r\\n" % (_e,)
    )
    _sys.stderr.flush()

try:
    import Quartz as _q
    _q.CGPreflightScreenCaptureAccess = lambda *a, **k: True
except Exception as _e:
    _sys.stderr.write(
        "[sitecustomize] CGPreflightScreenCaptureAccess stub install FAILED: %r\\n" % (_e,)
    )
    _sys.stderr.flush()
'''


def _engine_args(output_dir: Path) -> dict:
    """Build the engine arg dict the way the daemon does.

    Delegates to the production ``build_engine_worker_args`` shared with
    ``Supervisor._worker_args`` so the arg shape stays in lockstep with the
    daemon. A minimal capture config (no video / audio / window data) keeps the
    spawned recording light; the screen reader runs unconditionally, which is all
    the screen-stall signal needs. The ``_*_q`` queue keys are created downstream
    by the engine worker command itself, so they are intentionally absent here.
    """
    from screencap.daemon.schema import RecordingStartRequest
    from screencap.daemon.supervisor import build_engine_worker_args

    req = RecordingStartRequest(
        name="scr104-dispatch",
        audio=False,
        capture_video=False,
        capture_images=True,
        capture_window_data=False,
        network=False,
        live_upload=False,
        cloud_intent=False,
        scrub_enabled=False,
        show_on_website=False,
    )
    return build_engine_worker_args(
        req, name="scr104-dispatch", capture_dir=output_dir
    )


def test_real_engine_worker_dispatch_emits_capture_unhealthy(tmp_path: Path) -> None:
    import screencap

    # If THIS process lacks Screen Recording permission, the engine's startup
    # preflight (session.py) short-circuits to permission_lost (elapsed=0.0)
    # before record()'s supervisor loop runs, so the health-tick path is
    # unreachable and the test can't prove the wiring. Skip rather than false-
    # green. CGPreflightScreenCaptureAccess is non-prompting and read-only.
    try:
        import Quartz
        if not Quartz.CGPreflightScreenCaptureAccess():
            pytest.skip(
                "this process lacks Screen Recording permission — the engine's "
                "preflight short-circuits to permission_lost before record()'s "
                "supervisor loop, so the health-tick path is unreachable here"
            )
    except Exception:
        pass  # PyObjC unavailable / call failed → let the test run and rely on the elapsed guard

    site_dir = tmp_path / "inject"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    out_dir = tmp_path / "rec"
    out_dir.mkdir()

    # Resolve the src root of the screencap under test so the subprocess imports
    # the same code (matters in a worktree where the editable install may point
    # elsewhere).
    src_dir = Path(screencap.__file__).resolve().parents[1]
    encoded = base64.b64encode(
        json.dumps(_engine_args(out_dir)).encode("utf-8")
    ).decode("ascii")

    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(site_dir), str(src_dir), os.environ.get("PYTHONPATH", "")) if p
        ),
        # Fire fast: no warmup gate, two consecutive stalled ticks before emit.
        "CAPTURE_HEALTH_WINDOW_SECS": "0.0",
        "CAPTURE_HEALTH_DEBOUNCE_TICKS": "2",
        # Lightest pipeline that still runs the screen reader.
        "RECORD_VIDEO": "false",
        "RECORD_IMAGES": "true",
        "RECORD_AUDIO": "false",
        "RECORD_WINDOW_DATA": "false",
    }

    # Build the argv via the production dispatch helper so the spawn matches the
    # daemon exactly. With no SCREENCAP_DAEMON_ENGINE_COMMAND override and a
    # non-frozen interpreter this is [sys.executable, "-m", "screencap",
    # "_engine-worker", encoded].
    from screencap.daemon.supervisor import _default_engine_command

    # Mirror the production engine spawn (daemon/supervisor.py _PopenEngineProcess).
    # stdin=DEVNULL is REQUIRED: run_recording_worker calls os.setpgrp(), and an
    # inherited controlling tty would put the new process group in the background
    # and kill the engine (SIGTTOU/SIGTTIN) on the first tty touch.
    proc = subprocess.Popen(
        _default_engine_command(encoded),
        cwd=str(src_dir.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    deadline = time.monotonic() + _DISPATCH_DEADLINE_SECS
    health_event: dict | None = None
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select(
                [proc.stderr], [], [], min(1.0, max(0.1, remaining))
            )
            if not ready:
                if proc.poll() is not None:
                    break  # engine exited without emitting a health event
                continue
            line = proc.stderr.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # loguru / pynput noise interleaved on stderr
            if event.get("type") in ("capture_unhealthy", "permission_lost"):
                health_event = event
                break
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    assert health_event is not None, (
        "the real _engine-worker entry point ran record()'s supervisor loop but "
        "emitted no capture-health event within the deadline — the health-tick "
        "wiring in record() (or the entry-point dispatch into it) is broken, even "
        "though the _capture_health_tick unit tests still pass"
    )
    # The stubbed screen reader (attempt climbs, output flat) is the engineered
    # signal; the in-process labeller normally attributes it to ``reader_stalled``.
    # A host that genuinely lacks Screen Recording can instead surface the terminal
    # ``permission_lost`` — both prove the supervisor loop ran the tick and emitted.
    if health_event["type"] == "capture_unhealthy":
        assert health_event["reader"] == "screen"
        assert health_event["reason"] == "reader_stalled"
    else:
        assert health_event["type"] == "permission_lost"
        assert health_event.get("elapsed", 0.0) > 0.0, (
            "permission_lost with elapsed==0.0 is the startup preflight short-circuit "
            "(session.py), not record()'s supervisor loop — the health wiring was not "
            "exercised, so this must not count as a pass"
        )
