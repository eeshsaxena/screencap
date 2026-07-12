"""Tests for the ``screencap clip`` CLI subcommand (SCR-219 U3).

The command orchestrates the U1+U2 engine (``engine.video.export_clip``) into a
single local ``.mp4`` for ``[start_ms, end_ms)``. It is the auth-free local verb
the Swift ``ClipExportController`` (U6) spawns. Two contracts are pinned here:

* the **stderr event stream** (``clip_started`` / ``clip_progress`` /
  ``clip_done`` / ``clip_failed``) the app parses to drive determinate progress,
  matching the ``UploadEventLine`` tolerant-reader channel/shape; and
* the **stdout JSON envelope** (``ok`` + ``reason`` + ``path``), which the Swift
  ``CLIClient`` decodes — so the command **always exits 0** and carries
  success/failure in the payload (a non-zero exit would make ``runJSONRaw``
  discard stdout; see
  ``docs/solutions/integration-issues/cli-json-envelope-nonzero-exit-discards-stdout-2026-07-02.md``).

The engine itself is unit-tested in ``tests/test_video_clip.py`` /
``tests/test_audio_clip.py``; here ``export_clip`` is stubbed so we exercise the
CLI's eligibility gate, event emission, reason mapping, and envelope shape
without encoding real video.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest
from click.testing import CliRunner

from screencap.cli import cli

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


# The recording's frozen video-start anchor (epoch seconds) and its ms form. The
# clip command subtracts this to turn the caller's ABSOLUTE epoch-ms clip range
# into the milliseconds-from-video-start that ``export_clip`` expects.
_ANCHOR_S = 1000.0
_ANCHOR_MS = round(_ANCHOR_S * 1000)  # 1_000_000


@pytest.fixture
def recordings_root(tmp_path, monkeypatch):
    """A recordings root wired so the clip command's ``resolve_recording_dir``
    (via ``config.get_recordings_dir``) finds test recordings under it."""
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr("screencap.config.get_recordings_dir", lambda: root)
    return root


def _make_clippable(root: Path, name: str, *, video_start_time: float = _ANCHOR_S) -> Path:
    """A local, non-stub recording with source video present (≥1 chunk_*.mp4).

    The chunk bytes are irrelevant here — ``export_clip`` is stubbed — so a
    placeholder file is enough to satisfy the ``isClippable`` gate. The
    ``recording`` row carries the ``video_start_time`` anchor the command reads to
    convert an absolute-epoch-ms range into engine-relative ms.
    """
    rec_dir = root / name
    rec_dir.mkdir()
    (rec_dir / "chunk_0001.mp4").write_bytes(b"fake chunk video")
    conn = sqlite3.connect(rec_dir / "recording.db")
    conn.execute("CREATE TABLE recording (video_start_time REAL, timestamp REAL)")
    conn.execute(
        "INSERT INTO recording (video_start_time, timestamp) VALUES (?, ?)",
        (video_start_time, 1716800000.0),
    )
    conn.commit()
    conn.close()
    return rec_dir


def _fake_export(record: list | None = None, steps=((1, 3), (2, 3), (3, 3))):
    """A stand-in for ``export_clip``: drives ``on_progress`` then writes a file.

    Mirrors the real signature exactly so a kwargs/positional mismatch in the
    command surfaces as a test failure.
    """

    def _export(recording_dir, start_ms, end_ms, out_path, *, lock_timeout=30.0, on_progress=None):
        if record is not None:
            record.append(
                {
                    "recording_dir": str(recording_dir),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "out_path": str(out_path),
                    "lock_timeout": lock_timeout,
                }
            )
        if on_progress is not None:
            for done, total in steps:
                on_progress(done, total)
        Path(out_path).write_bytes(b"clip-data")
        return Path(out_path)

    return _export


def _raising_export(exc: BaseException):
    def _export(*_a, **_k):
        raise exc

    return _export


def _events(result) -> list[dict]:
    """Parse the JSON event lines the command wrote to stderr."""
    out: list[dict] = []
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "type" in obj:
            out.append(obj)
    return out


def _invoke(root, name, out_path, *, start=_ANCHOR_MS + 1000, end=_ANCHOR_MS + 5000, extra=None):
    args = [
        "clip",
        name,
        "--start-ms",
        str(start),
        "--end-ms",
        str(end),
        "--out",
        str(out_path),
        "--json",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(cli, args)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_emits_clip_done_and_ok_envelope(recordings_root, tmp_path):
    _make_clippable(recordings_root, "rec-ok")
    out = tmp_path / "clip.mp4"
    calls: list = []

    with mock.patch("screencap.engine.video.export_clip", _fake_export(calls)):
        result = _invoke(recordings_root, "rec-ok", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["reason"] is None
    assert payload["path"] == str(out)
    assert out.exists(), "the clip file must be written"

    # export_clip received the range CONVERTED to milliseconds-from-video-start:
    # the absolute epoch ms the caller passed minus the recording's anchor.
    assert len(calls) == 1
    assert calls[0]["start_ms"] == 1000  # (_ANCHOR_MS + 1000) - _ANCHOR_MS
    assert calls[0]["end_ms"] == 5000  # (_ANCHOR_MS + 5000) - _ANCHOR_MS
    assert calls[0]["out_path"] == str(out)
    assert calls[0]["recording_dir"].endswith("/rec-ok")

    # The envelope still reports the ABSOLUTE values the Swift caller correlates on.
    assert payload["start_ms"] == _ANCHOR_MS + 1000
    assert payload["end_ms"] == _ANCHOR_MS + 5000

    # Event stream: started then done, on the stderr channel.
    kinds = [e["type"] for e in _events(result)]
    assert "clip_started" in kinds
    assert "clip_done" in kinds
    done = next(e for e in _events(result) if e["type"] == "clip_done")
    assert done["path"] == str(out)


def test_progress_events_are_determinate(recordings_root, tmp_path):
    """R10: clip_progress carries frames_done / frames_total from on_progress."""
    _make_clippable(recordings_root, "rec-prog")
    out = tmp_path / "clip.mp4"

    with mock.patch(
        "screencap.engine.video.export_clip",
        _fake_export(steps=((2, 8), (5, 8), (8, 8))),
    ):
        result = _invoke(recordings_root, "rec-prog", out)

    assert result.exit_code == 0, result.output
    progress = [e for e in _events(result) if e["type"] == "clip_progress"]
    assert progress, "at least one determinate progress event must be emitted"
    for e in progress:
        assert isinstance(e["frames_done"], int)
        assert isinstance(e["frames_total"], int)
    assert (progress[0]["frames_done"], progress[0]["frames_total"]) == (2, 8)
    assert (progress[-1]["frames_done"], progress[-1]["frames_total"]) == (8, 8)


def test_lock_timeout_forwarded_to_engine(recordings_root, tmp_path):
    _make_clippable(recordings_root, "rec-lt")
    out = tmp_path / "clip.mp4"
    calls: list = []

    with mock.patch("screencap.engine.video.export_clip", _fake_export(calls)):
        result = _invoke(
            recordings_root, "rec-lt", out, extra=["--lock-timeout", "5.0"]
        )

    assert result.exit_code == 0, result.output
    assert calls[0]["lock_timeout"] == 5.0


def test_absolute_ms_converted_to_relative_before_export(recordings_root, tmp_path):
    """Cross-unit anchor conversion: the caller passes ABSOLUTE epoch ms (what
    Swift's ``ClipRange`` and the ``review-data`` verb speak); the command reads
    the recording's ``video_start_time`` anchor and hands ``export_clip``
    milliseconds-from-video-start. Concretely: anchor=1000.0s (1_000_000 ms),
    ``--start-ms 1_005_000`` → 5_000, ``--end-ms 1_010_000`` → 10_000.
    """
    _make_clippable(recordings_root, "rec-conv", video_start_time=1000.0)
    out = tmp_path / "clip.mp4"
    calls: list = []

    with mock.patch("screencap.engine.video.export_clip", _fake_export(calls)):
        result = _invoke(
            recordings_root, "rec-conv", out, start=1_005_000, end=1_010_000
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True

    # export_clip sees the RELATIVE range (absolute minus the 1_000_000 ms anchor).
    assert calls[0]["start_ms"] == 5_000
    assert calls[0]["end_ms"] == 10_000

    # The envelope + events keep the ABSOLUTE values the app correlates on.
    assert payload["start_ms"] == 1_005_000
    assert payload["end_ms"] == 1_010_000
    started = next(e for e in _events(result) if e["type"] == "clip_started")
    assert started["start_ms"] == 1_005_000
    assert started["end_ms"] == 1_010_000


# ---------------------------------------------------------------------------
# Failure shapes — every one exits 0 with a typed reason (envelope carries it)
# ---------------------------------------------------------------------------


def test_no_frames_in_range(recordings_root, tmp_path):
    from screencap.engine.video import NoFramesInRangeError

    _make_clippable(recordings_root, "rec-empty")
    out = tmp_path / "clip.mp4"

    with mock.patch(
        "screencap.engine.video.export_clip",
        _raising_export(NoFramesInRangeError("no frames")),
    ):
        result = _invoke(recordings_root, "rec-empty", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "no_frames_in_range"
    assert not out.exists(), "no file on a failed export"
    failed = next(e for e in _events(result) if e["type"] == "clip_failed")
    assert failed["reason"] == "no_frames_in_range"


def test_masked_video_required(recordings_root, tmp_path):
    """AE3: a flag-ON recording fails closed with masked_video_required."""
    from screencap.engine.video import MaskedVideoRequiredError

    _make_clippable(recordings_root, "rec-masked")
    out = tmp_path / "clip.mp4"

    with mock.patch(
        "screencap.engine.video.export_clip",
        _raising_export(MaskedVideoRequiredError("flag on")),
    ):
        result = _invoke(recordings_root, "rec-masked", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "masked_video_required"
    assert not out.exists()
    failed = next(e for e in _events(result) if e["type"] == "clip_failed")
    assert failed["reason"] == "masked_video_required"


def test_clip_busy_is_retryable(recordings_root, tmp_path):
    """Lock contention surfaces as a retryable clip_busy (exit 0)."""
    from screencap.terminal_stage import TerminalStageBusy

    _make_clippable(recordings_root, "rec-busy")
    out = tmp_path / "clip.mp4"

    with mock.patch(
        "screencap.engine.video.export_clip",
        _raising_export(TerminalStageBusy("held")),
    ):
        result = _invoke(recordings_root, "rec-busy", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "clip_busy"
    assert payload["retryable"] is True
    failed = next(e for e in _events(result) if e["type"] == "clip_failed")
    assert failed["reason"] == "clip_busy"
    assert failed["retryable"] is True


def test_generic_engine_failure_maps_to_trim_failed(recordings_root, tmp_path):
    from screencap.engine.video import ClipExportError

    _make_clippable(recordings_root, "rec-trim")
    out = tmp_path / "clip.mp4"

    with mock.patch(
        "screencap.engine.video.export_clip",
        _raising_export(ClipExportError("encoder blew up")),
    ):
        result = _invoke(recordings_root, "rec-trim", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "trim_failed"


def test_unexpected_exception_maps_to_trim_failed(recordings_root, tmp_path):
    """A non-taxonomy error must still exit 0 with the catch-all reason, never
    a raw traceback that a non-zero exit would hide from the Swift decoder."""
    _make_clippable(recordings_root, "rec-boom")
    out = tmp_path / "clip.mp4"

    with mock.patch(
        "screencap.engine.video.export_clip",
        _raising_export(RuntimeError("something unexpected")),
    ):
        result = _invoke(recordings_root, "rec-boom", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "trim_failed"


# ---------------------------------------------------------------------------
# Eligibility (isClippable) — engine is never called for an ineligible recording
# ---------------------------------------------------------------------------


def test_missing_recording_is_not_eligible(recordings_root, tmp_path):
    out = tmp_path / "clip.mp4"
    export_spy = mock.MagicMock()

    with mock.patch("screencap.engine.video.export_clip", export_spy):
        result = _invoke(recordings_root, "does-not-exist", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "not_eligible"
    export_spy.assert_not_called()
    assert not out.exists()
    failed = next(e for e in _events(result) if e["type"] == "clip_failed")
    assert failed["reason"] == "not_eligible"
    # The export never started, so no clip_started event precedes the failure.
    assert "clip_started" not in [e["type"] for e in _events(result)]


def test_stub_recording_without_video_is_not_eligible(recordings_root, tmp_path):
    """An uploaded-then-evicted stub (DB present, no chunk_*.mp4) can't be
    clipped — the engine is never invoked."""
    rec_dir = recordings_root / "rec-stub"
    rec_dir.mkdir()
    conn = sqlite3.connect(rec_dir / "recording.db")
    conn.execute("CREATE TABLE recording (timestamp REAL)")
    conn.commit()
    conn.close()
    out = tmp_path / "clip.mp4"
    export_spy = mock.MagicMock()

    with mock.patch("screencap.engine.video.export_clip", export_spy):
        result = _invoke(recordings_root, "rec-stub", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["reason"] == "not_eligible"
    export_spy.assert_not_called()


def test_missing_anchor_is_not_eligible(recordings_root, tmp_path):
    """A recording with source video but no readable video-start anchor (no
    recording.db) can't be range-converted — not_eligible, and the engine is
    never reached (no clip_started precedes the failure)."""
    rec_dir = recordings_root / "rec-noanchor"
    rec_dir.mkdir()
    (rec_dir / "chunk_0001.mp4").write_bytes(b"fake chunk video")
    out = tmp_path / "clip.mp4"
    export_spy = mock.MagicMock()

    with mock.patch("screencap.engine.video.export_clip", export_spy):
        result = _invoke(recordings_root, "rec-noanchor", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "not_eligible"
    export_spy.assert_not_called()
    assert not out.exists()
    assert "clip_started" not in [e["type"] for e in _events(result)]


def test_traversal_name_is_not_eligible(recordings_root, tmp_path):
    out = tmp_path / "clip.mp4"
    export_spy = mock.MagicMock()

    with mock.patch("screencap.engine.video.export_clip", export_spy):
        result = _invoke(recordings_root, "../escape", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["reason"] == "not_eligible"
    export_spy.assert_not_called()


def test_already_uploaded_recording_with_video_is_still_clippable(recordings_root, tmp_path):
    """KTD5: unlike isUploadEligible's !uploaded clause, isClippable keys on
    local video presence — an already-shared recording (chunks still on disk)
    is clippable."""
    rec_dir = _make_clippable(recordings_root, "rec-shared")
    # Mark it uploaded (legacy status marker) — must NOT block the clip.
    (rec_dir / ".upload_status.json").write_text("{}")
    out = tmp_path / "clip.mp4"

    with mock.patch("screencap.engine.video.export_clip", _fake_export()):
        result = _invoke(recordings_root, "rec-shared", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert out.exists()


# ---------------------------------------------------------------------------
# Cancel / SIGTERM (SCR-219) — the handler must run the engine's temp cleanup
# ---------------------------------------------------------------------------


def test_sigterm_runs_temp_cleanup_and_reports_cancelled(recordings_root, tmp_path):
    """A SIGTERM (Swift Cancel / window-close / watchdog) must route through the
    installed handler so ``export_clip``'s ``except BaseException`` cleanup runs —
    unlinking the full-size video-only intermediate instead of orphaning it — and
    land a terminal ``clip_failed(cancelled)`` with exit 0.

    The fake ``export_clip`` mirrors the real engine: it creates the intermediate,
    then simulates the OS delivering SIGTERM by invoking whatever handler
    ``clip_cmd`` installed, and unlinks the temp on the resulting BaseException. If
    ``clip_cmd`` installed NO handler (the bug), ``getsignal`` returns ``SIG_DFL``
    (an int) and calling it raises ``TypeError`` — the test fails loudly.
    """
    _make_clippable(recordings_root, "rec-cancel")
    out = tmp_path / "clip.mp4"
    tmp_holder: list[Path] = []
    sigterm_before = signal.getsignal(signal.SIGTERM)

    def _cancelling_export(
        recording_dir, start_ms, end_ms, out_path, *, lock_timeout=30.0, on_progress=None
    ):
        op = Path(out_path)
        temp = op.parent / f".clipvid.mp4.{os.getpid()}.{uuid4().hex}.tmp"
        temp.write_bytes(b"partial video-only intermediate")
        tmp_holder.append(temp)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)  # emits clip_failed(cancelled) + raises
            return op  # unreached — the handler raises
        except BaseException:
            temp.unlink(missing_ok=True)  # mirrors export_clip's real cleanup
            raise

    with mock.patch("screencap.engine.video.export_clip", _cancelling_export):
        result = _invoke(recordings_root, "rec-cancel", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "cancelled"
    assert payload["retryable"] is False
    assert not out.exists(), "no deliverable on a cancelled export"

    # The intermediate was reclaimed by export_clip's BaseException cleanup — which
    # only ran because clip_cmd installed a SIGTERM handler that RAISES.
    assert tmp_holder, "the fake export never created its intermediate"
    assert not tmp_holder[0].exists(), "the intermediate temp was orphaned on SIGTERM"

    # Exactly one terminal clip_failed(cancelled), on the stderr channel, after the
    # clip_started the export emitted.
    kinds = [e["type"] for e in _events(result)]
    assert "clip_started" in kinds
    failed = [e for e in _events(result) if e["type"] == "clip_failed"]
    assert len(failed) == 1, f"expected exactly one clip_failed, got {failed}"
    assert failed[0]["reason"] == "cancelled"
    assert failed[0]["retryable"] is False

    # The previous SIGTERM disposition is restored in the finally (no leak).
    assert signal.getsignal(signal.SIGTERM) == sigterm_before


# ---------------------------------------------------------------------------
# Auth-free (KTD5)
# ---------------------------------------------------------------------------


def test_no_auth_required(recordings_root, tmp_path):
    """The clip verb is local + auth-free: it must succeed with no account, and
    must never consult the auth/token surface."""
    from screencap import auth

    _make_clippable(recordings_root, "rec-noauth")
    out = tmp_path / "clip.mp4"

    with mock.patch("screencap.engine.video.export_clip", _fake_export()), mock.patch.object(
        auth, "get_id_token", side_effect=auth.NotSignedIn("no account")
    ) as token_spy:
        result = _invoke(recordings_root, "rec-noauth", out)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert out.exists()
    token_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Envelope stability across every shape (the Swift decoder relies on this)
# ---------------------------------------------------------------------------


def test_json_envelope_is_stable_and_parseable_across_shapes(recordings_root, tmp_path):
    from screencap.engine.video import (
        ClipExportError,
        MaskedVideoRequiredError,
        NoFramesInRangeError,
    )
    from screencap.terminal_stage import TerminalStageBusy

    _make_clippable(recordings_root, "rec-shape")

    shapes = [
        ("ok", _fake_export(), True, None),
        ("no_frames_in_range", _raising_export(NoFramesInRangeError("x")), False, "no_frames_in_range"),
        ("masked", _raising_export(MaskedVideoRequiredError("x")), False, "masked_video_required"),
        ("busy", _raising_export(TerminalStageBusy("x")), False, "clip_busy"),
        ("trim", _raising_export(ClipExportError("x")), False, "trim_failed"),
    ]

    for i, (label, stub, ok, reason) in enumerate(shapes):
        out = tmp_path / f"clip-{i}.mp4"
        with mock.patch("screencap.engine.video.export_clip", stub):
            result = _invoke(recordings_root, "rec-shape", out)

        assert result.exit_code == 0, f"{label}: {result.output}"
        # stdout is a single clean JSON object (events are isolated on stderr).
        payload = json.loads(result.stdout)
        assert payload["ok"] is ok, label
        assert payload["schema_version"] >= 1
        assert payload["recording"] == "rec-shape"
        if ok:
            assert payload["reason"] is None
            assert payload["path"]
        else:
            assert payload["reason"] == reason, label
            assert "retryable" in payload
