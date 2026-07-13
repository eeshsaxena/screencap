"""Tests for clip-range-scoped consent prep + scrub cache-reuse (SCR-219, U4).

``prepare_review_data`` gains optional ``clip_start_ms`` / ``clip_end_ms``
inputs so the consent Review can be scoped to a clip's ``[start, end)`` range
(KTD4, R7) and can reuse a fresh ``<name>-scrubbed`` copy instead of
re-running the whole-recording scrub per clip.

Contract exercised here:

- Clip range present → the masked-screenshot truth-set and the events are
  filtered to ``[start, end)`` (nothing outside the range reaches the consent
  set), and an additive ``clip_video_capture_blocked_only`` honesty flag is
  emitted.
- Clip range absent → the envelope is byte-for-byte today's shape (no new key,
  no filtering) — the regression guard.
- Cache-reuse → a second prepare against a current ``.scrub_complete`` skips
  ``scrub_recording`` entirely.
- Empty range → an empty, structured consent set, never a crash.

All clip-range inputs are epoch **milliseconds** (matching the Swift Day
timeline's ``currentDayMs`` / ``pendingSeekMs``, which are epoch-seconds ×
1000); the recording DB and screenshot timestamps are epoch **seconds**, so
the module divides by 1000 to anchor the two spaces.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner
from PIL import Image

from screencap.cli import cli
from screencap.engine import utils
from screencap.engine.video import VideoWriter
from screencap.review import prepare_review_data

BASE_TS = 1_700_000_000.0


# ---------------------------------------------------------------------------
# Helpers / fixtures (mirror tests/test_cli_review_data.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _init_timestamp():
    """VideoWriter relies on the engine timestamp system being initialized."""
    utils.set_start_time(time.time())


def _write_video(path: Path) -> None:
    """Write a real, AVKit-safe (yuv420p → no remediation) solid-color video."""
    writer = VideoWriter(str(path), width=32, height=32, fps=24, pix_fmt="yuv420p")
    base = time.time()
    for i in range(3):
        writer.write_frame(Image.new("RGB", (32, 32), color=(0, 0, 200)), base + i / 24)
    writer.close()


def _write_screenshot(rec_dir: Path, ts: float) -> None:
    shots = rec_dir / "screenshots"
    shots.mkdir(exist_ok=True)
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(shots / f"{ts}.jpg")


def _build_db(rec_dir: Path, base_ts: float, clicks: list[tuple[float, int]]) -> None:
    """Create a real engine recording.db with click action pairs.

    ``clicks`` is ``[(offset_seconds, mouse_x), ...]``; each entry becomes a
    press+release pair (0.02s apart) that ``export_chunk_events`` folds into one
    ``MouseClickEvent`` carrying ``x = mouse_x`` at the press timestamp.
    """
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(rec_dir / "recording.db"))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": base_ts,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    for offset, x in clicks:
        for pressed, t in ((1, base_ts + offset), (0, base_ts + offset + 0.02)):
            crud.insert_action_event(session, recording, t, {
                "name": "click", "mouse_x": x, "mouse_y": 10,
                "mouse_button_name": "left", "mouse_pressed": pressed,
            })
    session.commit()
    session.close()
    engine.dispose()


def _make_source(
    root: Path,
    name: str,
    *,
    clicks: list[tuple[float, int]] | None = None,
    shot_offsets: list[float] | None = None,
    base_ts: float = BASE_TS,
) -> Path:
    """Create a full source recording dir: video + db + events + screenshots."""
    rec_dir = root / name
    rec_dir.mkdir()
    _write_video(rec_dir / "video.mp4")
    _build_db(rec_dir, base_ts, clicks or [])
    # Pre-write events.jsonl so ensure_canonical_events trusts it (no re-export).
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")
    for off in (shot_offsets or []):
        _write_screenshot(rec_dir, base_ts + off)
    return rec_dir


@pytest.fixture
def recordings_root(tmp_path, monkeypatch, request):
    """A recordings root wired so ``resolve_recording_dir`` finds it, with a fast
    fake scrub (copytree, no NER) unless the test is marked ``real_scrub``."""
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr("screencap.config.get_recordings_dir", lambda: root)
    monkeypatch.setattr("screencap.scrubber.get_recordings_dir", lambda: root)
    if not request.node.get_closest_marker("real_scrub"):
        monkeypatch.setattr("screencap.scrubber.scrub_recording", _fake_scrub(root))
        monkeypatch.setattr(
            "screencap.recovery._recover_chunk_metadata", lambda *a, **k: None,
        )
    return root


def _fake_scrub(root: Path):
    """Fast stand-in for ``scrub_recording``: copytree source → ``<name>-scrubbed``
    with media filtered out (recording.db, events*.jsonl, screenshots/ survive)."""
    import shutil as _sh

    def _fake(name, pii_engine=None, *, cloud_bound_recovery=False, _already_locked=False):
        from screencap.scrubber import _SKIP_EXTENSIONS, _SKIP_FILES, ScrubResult

        src = root / name
        dst = root / f"{name}-scrubbed"
        if dst.exists():
            _sh.rmtree(dst)

        def _ignore(_d, entries):
            return {
                e for e in entries
                if e in _SKIP_FILES or Path(e).suffix in _SKIP_EXTENSIONS
            }

        _sh.copytree(src, dst, ignore=_ignore)
        return ScrubResult(output_dir=dst)

    return _fake


def _ms(seconds: float) -> int:
    """Epoch seconds → epoch milliseconds (the clip-range input space)."""
    return int(round(seconds * 1000))


def _events_in(envelope: dict) -> list[dict]:
    """Parse the (single) scoped events file, returning event lines (no meta).

    The meta header line carries ``_meta`` and no ``timestamp``; real events do.
    """
    events = []
    for ln in Path(envelope["events_path"]).read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        if "timestamp" in rec:
            events.append(rec)
    return events


# ---------------------------------------------------------------------------
# Clip-range scoping: screenshots + events filtered to [start, end)
# ---------------------------------------------------------------------------


def test_clip_range_filters_screenshots_and_events(recordings_root):
    """Covers R7: clip-range inputs filter both the masked-screenshot truth-set
    and the events to ``[start, end)`` — nothing outside the range in the
    consent set."""
    _make_source(
        recordings_root, "rec-clip",
        clicks=[(1.0, 11), (5.0, 55), (9.0, 99), (15.0, 150)],
        shot_offsets=[1.0, 5.0, 9.0, 15.0],
    )

    envelope = prepare_review_data(
        "rec-clip",
        clip_start_ms=_ms(BASE_TS + 4.0),
        clip_end_ms=_ms(BASE_TS + 10.0),
    )

    assert envelope["ok"] is True

    # Screenshots: only the two in-range frames (base+5, base+9).
    shot_ts = sorted(
        float(Path(p).stem) for p in envelope["screenshots"]
    )
    assert shot_ts == [BASE_TS + 5.0, BASE_TS + 9.0]

    # Events: only the two in-range clicks (x=55, x=99); nothing outside.
    click_xs = sorted(
        e["x"] for e in _events_in(envelope) if e.get("type") == "mouse.singleclick"
    )
    assert click_xs == [55, 99]
    # Every emitted event's timestamp lies inside the half-open range.
    for e in _events_in(envelope):
        assert (BASE_TS + 4.0) <= e["timestamp"] < (BASE_TS + 10.0)


def test_clip_range_events_come_from_scrubbed_copy(recordings_root):
    """The scoped events file lives under ``<name>-scrubbed`` (reviewed ==
    uploaded), not the source dir."""
    _make_source(
        recordings_root, "rec-clip-loc",
        clicks=[(5.0, 55)], shot_offsets=[5.0],
    )
    envelope = prepare_review_data(
        "rec-clip-loc",
        clip_start_ms=_ms(BASE_TS + 4.0),
        clip_end_ms=_ms(BASE_TS + 10.0),
    )
    assert "/rec-clip-loc-scrubbed/" in envelope["events_path"]
    assert all("/rec-clip-loc-scrubbed/" in p for p in envelope["events_paths"])


# ---------------------------------------------------------------------------
# Honesty flag: present when a clip range is given, absent otherwise
# ---------------------------------------------------------------------------


def test_honesty_flag_present_with_clip_range(recordings_root):
    """A clip range emits the additive ``clip_video_capture_blocked_only`` flag."""
    _make_source(recordings_root, "rec-flag", clicks=[(5.0, 55)], shot_offsets=[5.0])
    envelope = prepare_review_data(
        "rec-flag",
        clip_start_ms=_ms(BASE_TS + 4.0),
        clip_end_ms=_ms(BASE_TS + 10.0),
    )
    assert envelope["clip_video_capture_blocked_only"] is True


def test_honesty_flag_absent_without_clip_range(recordings_root):
    """No clip range → the honesty flag key is absent (not False) so the
    whole-recording envelope is unchanged."""
    _make_source(recordings_root, "rec-noflag", clicks=[(5.0, 55)], shot_offsets=[5.0])
    envelope = prepare_review_data("rec-noflag")
    assert "clip_video_capture_blocked_only" not in envelope


# ---------------------------------------------------------------------------
# Regression guard: no clip range → envelope is exactly today's shape
# ---------------------------------------------------------------------------


def test_no_clip_range_matches_today(recordings_root):
    """No clip range → all screenshots, full event set, no clip key: byte-for-byte
    today's whole-recording envelope."""
    _make_source(
        recordings_root, "rec-whole",
        clicks=[(1.0, 11), (5.0, 55), (15.0, 150)],
        shot_offsets=[1.0, 5.0, 15.0],
    )

    envelope = prepare_review_data("rec-whole")

    assert envelope["ok"] is True
    # No clip-scoping keys leak into the whole-recording envelope.
    assert "clip_video_capture_blocked_only" not in envelope
    # All three screenshots present (unfiltered).
    shot_ts = sorted(float(Path(p).stem) for p in envelope["screenshots"])
    assert shot_ts == [BASE_TS + 1.0, BASE_TS + 5.0, BASE_TS + 15.0]
    # Events resolve to the scrubbed dir's own file set (not a scoped clip file).
    assert all(Path(p).name == "events.jsonl" for p in envelope["events_paths"])
    assert not any(".clip_review_events" in p for p in envelope["events_paths"])
    # The full envelope key set is unchanged from today.
    assert set(envelope) == {
        "ok", "schema_version", "video_path", "events_path", "events_paths",
        "screenshots", "redaction", "coverage", "started_at", "duration_seconds",
        "timing_status", "timing_error", "video_pixfmt_remediated",
    }


# ---------------------------------------------------------------------------
# Cache-reuse: a current .scrub_complete skips scrub_recording
# ---------------------------------------------------------------------------


def test_cache_reuse_skips_rescrub(recordings_root):
    """A fresh ``<name>-scrubbed`` with a current ``.scrub_complete`` is reused —
    ``scrub_recording`` is NOT called again."""
    from screencap.scrubber import _write_scrub_sentinel

    rec_dir = _make_source(recordings_root, "rec-reuse", clicks=[(5.0, 55)], shot_offsets=[5.0])

    # Pre-build a reusable scrubbed copy with a valid completion sentinel.
    scrubbed = recordings_root / "rec-reuse-scrubbed"
    scrubbed.mkdir()
    (scrubbed / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")
    _write_screenshot(scrubbed, BASE_TS + 5.0)
    _write_scrub_sentinel(scrubbed, rec_dir, cloud_bound_recovery=True)

    with mock.patch("screencap.scrubber.scrub_recording") as scrub_spy:
        envelope = prepare_review_data("rec-reuse")

    scrub_spy.assert_not_called()
    assert envelope["ok"] is True
    assert "/rec-reuse-scrubbed/" in envelope["events_path"]


def test_no_reuse_when_sentinel_missing_runs_scrub(recordings_root):
    """Without a ``.scrub_complete`` sentinel the scrubbed copy is not reusable —
    the scrub runs (control for the reuse test)."""
    _make_source(recordings_root, "rec-noreuse", clicks=[(5.0, 55)], shot_offsets=[5.0])

    calls: list[str] = []
    real_fake = _fake_scrub(recordings_root)

    def spy(name, *a, **k):
        calls.append(name)
        return real_fake(name)

    with mock.patch("screencap.scrubber.scrub_recording", side_effect=spy):
        prepare_review_data("rec-noreuse")

    assert calls == ["rec-noreuse"], "no sentinel → scrub must run"


# ---------------------------------------------------------------------------
# Empty / degenerate ranges: structured empty set, no crash
# ---------------------------------------------------------------------------


def test_clip_range_with_no_screenshots_is_empty_not_error(recordings_root):
    """A clip range that overlaps no screenshots yields an explicit empty consent
    set — structured, no crash."""
    _make_source(
        recordings_root, "rec-empty",
        clicks=[(1.0, 11)], shot_offsets=[1.0],  # everything before the range
    )

    envelope = prepare_review_data(
        "rec-empty",
        clip_start_ms=_ms(BASE_TS + 100.0),
        clip_end_ms=_ms(BASE_TS + 200.0),
    )

    assert envelope["ok"] is True
    assert envelope["screenshots"] == []
    # The scoped events file exists but carries no in-range events.
    assert _events_in(envelope) == []
    assert envelope["clip_video_capture_blocked_only"] is True


def test_clip_range_requires_both_bounds(recordings_root):
    """Passing only one bound is a caller error surfaced as a structured
    ReviewPrepareError, not a silent whole-recording fallback."""
    from screencap.review import ReviewPrepareError

    _make_source(recordings_root, "rec-onebound", clicks=[(5.0, 55)], shot_offsets=[5.0])

    with pytest.raises(ReviewPrepareError):
        prepare_review_data("rec-onebound", clip_start_ms=_ms(BASE_TS + 4.0))


# ---------------------------------------------------------------------------
# CLI plumbing: --clip-start-ms / --clip-end-ms reach prepare_review_data
# ---------------------------------------------------------------------------


def test_cli_clip_range_options_plumbed(recordings_root):
    """``screencap review-data --clip-start-ms --clip-end-ms`` scopes the
    envelope (honesty flag present, screenshots filtered)."""
    _make_source(
        recordings_root, "rec-cli-clip",
        clicks=[(5.0, 55)], shot_offsets=[1.0, 5.0, 15.0],
    )

    result = CliRunner().invoke(cli, [
        "review-data", "--json", "rec-cli-clip",
        "--clip-start-ms", str(_ms(BASE_TS + 4.0)),
        "--clip-end-ms", str(_ms(BASE_TS + 10.0)),
    ])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["clip_video_capture_blocked_only"] is True
    shot_ts = sorted(float(Path(p).stem) for p in payload["screenshots"])
    assert shot_ts == [BASE_TS + 5.0]
