"""Tests for the ``screencap inspect-data`` CLI subcommand and the
``screencap.review.prepare_inspect_data`` orchestration (native read-only
inspect window, U1).

``inspect-data`` is the no-scrub sibling of ``review-data``: the SwiftUI shell
calls it before opening the read-only inspect window (from a search result or a
recordings-list click — "just looking", not uploading). It reads the LOCAL
recording from the ORIGINAL dir with no scrub, so these tests assert the
distinct contract: the no-scrub invariant (no ``-scrubbed`` dir, the scrub lock
never acquired), original-dir event resolution (per-chunk and combined),
tolerated-empty events (video-first), null redaction/coverage, path containment,
and the structural failure flavors. The shared video/timing core is already
covered by ``test_cli_review_data.py``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner
from PIL import Image

from screencap.cli import cli
from screencap.engine import utils
from screencap.engine.video import VideoWriter
from screencap.review import (
    REVIEW_SCHEMA_VERSION,
    ReviewPrepareError,
    prepare_inspect_data,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _init_timestamp():
    """VideoWriter relies on the engine timestamp system being initialized."""
    utils.set_start_time(time.time())


def _write_video(path: Path, color: tuple[int, int, int] = (0, 0, 200)) -> None:
    """Write a small, already-AVKit-safe (yuv420p) H.264 video via VideoWriter."""
    base = time.time()
    writer = VideoWriter(str(path), width=64, height=64, fps=24, pix_fmt="yuv420p")
    for i in range(5):
        writer.write_frame(Image.new("RGB", (64, 64), color=color), base + i / 24)
    writer.close()


def _make_recording(root: Path, name: str, *, with_db: bool = True) -> Path:
    """Create a recording dir under ``root`` (optionally with a timing DB)."""
    rec_dir = root / name
    rec_dir.mkdir()
    if with_db:
        conn = sqlite3.connect(rec_dir / "recording.db")
        conn.execute("CREATE TABLE recording (timestamp REAL)")
        conn.execute("INSERT INTO recording VALUES (1716800000.0)")
        conn.commit()
        conn.close()
    return rec_dir


@pytest.fixture
def recordings_root(tmp_path, monkeypatch):
    """A recordings root wired so ``resolve_recording_dir`` and the containment
    check both see the test tree."""
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr("screencap.config.get_recordings_dir", lambda: root)
    return root


# ---------------------------------------------------------------------------
# prepare_inspect_data — reads the original dir, never scrubs
# ---------------------------------------------------------------------------


def test_inspect_reads_original_dir_and_never_scrubs(recordings_root):
    """Happy path: the envelope's video + events resolve under the ORIGINAL
    recording dir (no ``-scrubbed`` sibling is produced), redaction/coverage are
    null, and screenshots is empty (video-first, no masked pane)."""
    rec_dir = _make_recording(recordings_root, "rec-inspect")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_inspect_data("rec-inspect")

    assert envelope["ok"] is True
    assert envelope["schema_version"] == REVIEW_SCHEMA_VERSION
    # Video + events live under the ORIGINAL dir, never a scrubbed copy.
    assert envelope["video_path"].endswith("/rec-inspect/video.mp4")
    assert envelope["events_path"].endswith("/rec-inspect/events.jsonl")
    assert all("-scrubbed" not in p for p in envelope["events_paths"])
    assert "-scrubbed" not in envelope["video_path"]
    # No scrubbed dir was produced anywhere under the root.
    assert list(recordings_root.glob("*-scrubbed")) == []
    # No upload-payload evidence on a "just looking" surface.
    assert envelope["redaction"] is None
    assert envelope["coverage"] is None
    assert envelope["screenshots"] == []


def test_no_scrub_invariant_lock_never_acquired(recordings_root):
    """The no-scrub invariant, durably: inspect-data must never call
    ``scrub_recording`` nor acquire ``recording_scrub_lock`` — a future refactor
    that pulled in the lock would block concurrent review/upload for the
    recording without scrubbing anything. Patch both to fail loudly if touched."""
    import contextlib

    rec_dir = _make_recording(recordings_root, "rec-nolock")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    @contextlib.contextmanager
    def forbidden_lock(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("inspect-data must not acquire recording_scrub_lock")
        yield

    def forbidden_scrub(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("inspect-data must not call scrub_recording")

    with mock.patch("screencap.scrubber.recording_scrub_lock", forbidden_lock), \
         mock.patch("screencap.scrubber.scrub_recording", forbidden_scrub):
        envelope = prepare_inspect_data("rec-nolock")

    assert envelope["ok"] is True
    assert list(recordings_root.glob("*-scrubbed")) == []


def test_chunked_events_resolve_from_original_dir(recordings_root):
    """A chunked recording's events are the per-chunk ``events_*.jsonl`` set in
    the ORIGINAL dir (the chunked-recording branch the doc-review flagged) — not
    a scrubbed copy, and not a null events_path."""
    rec_dir = _make_recording(recordings_root, "rec-chunked")
    _write_video(rec_dir / "video.mp4")
    for idx in (0, 1):
        (rec_dir / f"events_{idx:04d}.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_inspect_data("rec-chunked")

    names = sorted(Path(p).name for p in envelope["events_paths"])
    assert names == ["events_0000.jsonl", "events_0001.jsonl"]
    assert all("/rec-chunked/" in p and "-scrubbed" not in p for p in envelope["events_paths"])


def test_nonchunked_events_exported_to_original_dir(recordings_root):
    """A non-chunked recording with no events.jsonl gets one exported into the
    ORIGINAL dir via the shared ``ensure_canonical_events`` gate (which the
    review/upload path also uses — so include_network=False is inherited, not a
    new decision), and the envelope points at it."""
    rec_dir = _make_recording(recordings_root, "rec-export")
    _write_video(rec_dir / "video.mp4")
    assert not (rec_dir / "events.jsonl").exists()

    def fake_export(rec_dir, output_path, exclude_moves, metadata, **kwargs):
        Path(output_path).write_text(json.dumps(metadata) + "\n")
        return 1

    with mock.patch("screencap.exporter.export_recording", side_effect=fake_export) as ex:
        envelope = prepare_inspect_data("rec-export")

    assert (rec_dir / "events.jsonl").exists(), "canonical export lands in the original dir"
    assert envelope["events_path"].endswith("/rec-export/events.jsonl")
    assert "-scrubbed" not in envelope["events_path"]
    # Shared gate → canonical upload config (the network-exclusion guarantee).
    assert ex.call_args.kwargs["exclude_moves"] is False
    # include_network parity with upload: ensure_canonical_events relies on
    # export_recording's cloud-safe `include_network=False` default, so the
    # inspect export must never opt into network rows the upload path excludes.
    # The kwarg is left at its default here, so assert the effective value is
    # False (absent ≡ False) rather than an explicit True.
    assert ex.call_args.kwargs.get("include_network", False) is False


def test_empty_events_is_tolerated_video_first(recordings_root):
    """Unlike review (which fails on no reviewable events), inspect is
    video-first: a recording whose event set is empty still yields ok=True with
    events_path=None / events_paths=[], so the operator can still watch it."""
    rec_dir = _make_recording(recordings_root, "rec-noevents")
    _write_video(rec_dir / "video.mp4")

    # No events.jsonl, no chunks; skip the export so none is conjured.
    with mock.patch("screencap.review._export_canonical_events", lambda _d: None):
        envelope = prepare_inspect_data("rec-noevents")

    assert envelope["ok"] is True
    assert envelope["events_paths"] == []
    assert envelope["events_path"] is None
    assert envelope["video_path"].endswith("/rec-noevents/video.mp4")


def test_events_export_failure_is_tolerated(recordings_root):
    """A failed events export (e.g. a corrupt recording.db) must not block
    looking at the video — it is logged to stderr and the window still opens
    with an empty event set, rather than failing the whole inspect."""
    rec_dir = _make_recording(recordings_root, "rec-exportfail")
    _write_video(rec_dir / "video.mp4")

    def boom(_d):
        raise RuntimeError("events export blew up")

    with mock.patch("screencap.review._export_canonical_events", side_effect=boom):
        envelope = prepare_inspect_data("rec-exportfail")

    assert envelope["ok"] is True
    assert envelope["events_paths"] == []
    assert envelope["video_path"].endswith("/rec-exportfail/video.mp4")


def test_events_export_failure_with_markup_does_not_crash(recordings_root):
    """SCR-117/169 parity: a *tolerated* events-export failure whose message
    carries Rich-markup metacharacters (a bracketed path / SQLite identifier)
    must not raise MarkupError out of prepare_inspect_data. Without escaping, the
    warning's `console.print` would raise MarkupError — which is not a
    ReviewPrepareError, so it would escape this tolerant handler and the CLI's
    envelope guard, turning a non-fatal export failure into a raw-traceback crash
    on the JSON channel."""
    from rich.errors import MarkupError

    rec_dir = _make_recording(recordings_root, "rec-markup")
    _write_video(rec_dir / "video.mp4")

    def boom(_d):
        # An unbalanced ``[/]`` is the dangerous case — it makes rich raise.
        raise RuntimeError("token=[/] in [secret] failed")

    with mock.patch("screencap.review._export_canonical_events", side_effect=boom):
        try:
            envelope = prepare_inspect_data("rec-markup")
        except MarkupError as e:  # pragma: no cover - the regression we guard against
            raise AssertionError(f"markup in a tolerated export failure crashed: {e}") from e

    assert envelope["ok"] is True
    assert envelope["events_paths"] == []


def test_nullable_timing_serialized_as_json_null(recordings_root):
    """SCR-102 parity: a playable recording with no action events still yields
    ok=True with null timing — the Swift readiness guard must not gate on it."""
    rec_dir = _make_recording(recordings_root, "rec-nometa")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    with mock.patch(
        "screencap.catalog._read_recording_meta", return_value=(None, None, "ok")
    ):
        envelope = prepare_inspect_data("rec-nometa")

    assert envelope["ok"] is True
    assert envelope["started_at"] is None
    assert envelope["duration_seconds"] is None
    assert envelope["timing_status"] == "ok"
    assert envelope["timing_error"] is False


# ---------------------------------------------------------------------------
# prepare_inspect_data — security / structural failure flavors
# ---------------------------------------------------------------------------


def test_path_outside_recordings_root_is_rejected(recordings_root, tmp_path):
    """Defense-in-depth: a symlinked event file that resolves OUTSIDE the
    recordings root is refused before any path is emitted (a symlink inside the
    dir could otherwise put an out-of-tree absolute path into the envelope)."""
    rec_dir = _make_recording(recordings_root, "rec-escape")
    _write_video(rec_dir / "video.mp4")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"_meta": True}) + "\n")
    (rec_dir / "events.jsonl").symlink_to(outside)

    with pytest.raises(ReviewPrepareError, match="outside the recordings root"):
        prepare_inspect_data("rec-escape")


def test_video_symlink_outside_recordings_root_is_rejected(recordings_root, tmp_path):
    """Defense-in-depth, video branch: a ``video.mp4`` symlinked to a real video
    OUTSIDE the recordings root is refused before its path is emitted. Mirrors
    the event-file containment test, but exercises the ``video_path`` arm of
    ``_assert_within_recordings_root`` — the AVKit player would otherwise be
    handed an out-of-tree absolute path."""
    rec_dir = _make_recording(recordings_root, "rec-videscape")
    # A real, AVKit-safe video living outside the root; the in-dir video.mp4 is a
    # symlink to it, so the resolved path escapes containment.
    outside = tmp_path / "outside.mp4"
    _write_video(outside)
    (rec_dir / "video.mp4").symlink_to(outside)

    with pytest.raises(ReviewPrepareError, match="outside the recordings root"):
        prepare_inspect_data("rec-videscape")


def test_no_video_is_distinct_error(recordings_root):
    """A recording with neither video.mp4 nor chunks yields the distinct 'no
    video to review' error — nothing to look at, so inspect cannot open."""
    _make_recording(recordings_root, "rec-novideo")  # DB only

    with pytest.raises(ReviewPrepareError, match="no video to review") as exc:
        prepare_inspect_data("rec-novideo")
    assert "can't process this video" not in str(exc.value)


def test_invalid_name_traversal_is_rejected(recordings_root):
    """A path-traversal name is rejected before any path work — distinct from a
    decode failure."""
    with pytest.raises(ReviewPrepareError) as exc:
        prepare_inspect_data("../escaping")
    assert "can't process this video" not in str(exc.value)


def test_missing_recording_is_distinct_error(recordings_root):
    with pytest.raises(ReviewPrepareError, match="not found"):
        prepare_inspect_data("does-not-exist")


# ---------------------------------------------------------------------------
# CLI command (`screencap inspect-data`)
# ---------------------------------------------------------------------------


def test_cli_emits_json_envelope(recordings_root):
    rec_dir = _make_recording(recordings_root, "rec-cli")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    result = CliRunner().invoke(cli, ["inspect-data", "--json", "rec-cli"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == REVIEW_SCHEMA_VERSION
    assert payload["video_path"].endswith("/rec-cli/video.mp4")
    assert payload["events_path"].endswith("/rec-cli/events.jsonl")
    assert payload["redaction"] is None
    assert payload["coverage"] is None


def test_cli_cant_process_emits_error_envelope(recordings_root):
    """A corrupt source surfaces ok=false + non-zero exit + the structural
    'can't process this video' message — never a raw traceback."""
    rec_dir = _make_recording(recordings_root, "rec-cli-bad")
    (rec_dir / "video.mp4").write_bytes(b"not a real mp4")

    result = CliRunner().invoke(cli, ["inspect-data", "--json", "rec-cli-bad"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["schema_version"] == REVIEW_SCHEMA_VERSION
    assert "can't process this video" in payload["error"]
    assert "ffmpeg" not in payload["error"].lower()


def test_cli_cant_process_is_not_retryable(recordings_root):
    """A genuine decode failure is NOT retryable — retrying a corrupt video will
    never succeed, so the envelope must not carry the retryable flag (only the
    transient 'still finalizing' busy condition does)."""
    rec_dir = _make_recording(recordings_root, "rec-cli-bad2")
    (rec_dir / "video.mp4").write_bytes(b"not a real mp4")

    result = CliRunner().invoke(cli, ["inspect-data", "--json", "rec-cli-bad2"])

    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload.get("retryable") is not True


# ---------------------------------------------------------------------------
# View-during-finalization race: a first view opened while the terminal stage
# holds the per-recording lock must be a TRANSIENT, retryable condition, not the
# corrupt-video failure that renders "Could not load this recording."
# ---------------------------------------------------------------------------


def test_inspect_busy_terminal_stage_raises_retryable_busy(recordings_root, monkeypatch):
    """When the ``terminal_lock`` is held (finalization still in flight),
    ``prepare_inspect_data`` raises the transient ``ReviewPrepareBusy`` — a
    subclass of ``ReviewPrepareError`` — rather than the corrupt-video
    ``can't process this video`` flavor. Regression for the
    view-during-finalization race."""
    from screencap import review as review_mod
    from screencap.review import ReviewPrepareBusy
    from screencap.terminal_stage import terminal_lock

    # Fail fast in the test rather than waiting the real inspect ceiling.
    monkeypatch.setattr(review_mod, "_INSPECT_LOCK_TIMEOUT", 0.1)

    rec_dir = _make_recording(recordings_root, "rec-busy")
    # A chunk with NO video.mp4 forces _ensure_single_video to acquire the lock
    # (it early-returns when video.mp4 already exists), reproducing a first view.
    _write_video(rec_dir / "chunk_0000.mp4")

    with terminal_lock("rec-busy"):
        with pytest.raises(ReviewPrepareBusy) as exc:
            prepare_inspect_data("rec-busy")

    # Transient, not corruption — the message must not read as a hard failure.
    assert "can't process this video" not in str(exc.value)
    assert "finalizing" in str(exc.value).lower()


def test_cli_busy_emits_retryable_envelope_with_zero_exit(recordings_root, monkeypatch):
    """The CLI translates ``ReviewPrepareBusy`` into ``ok=false`` +
    ``retryable=true`` and — critically — exits ZERO.

    The SwiftUI ``CLIClient.runJSONRaw`` throws on a non-zero exit and DISCARDS
    stdout, so a non-zero exit here would drop the retryable envelope before the
    shell could decode it and auto-retry — reintroducing the exact 'Could not
    load this recording.' race this fix removes. Exit 0 is the contract that lets
    the transient envelope reach the decoder."""
    from screencap import review as review_mod
    from screencap.terminal_stage import terminal_lock

    monkeypatch.setattr(review_mod, "_INSPECT_LOCK_TIMEOUT", 0.1)

    rec_dir = _make_recording(recordings_root, "rec-cli-busy")
    _write_video(rec_dir / "chunk_0000.mp4")

    with terminal_lock("rec-cli-busy"):
        result = CliRunner().invoke(cli, ["inspect-data", "--json", "rec-cli-busy"])

    assert result.exit_code == 0, "a transient/retryable result must exit 0 so stdout survives"
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["retryable"] is True
    assert "can't process this video" not in payload["error"]


# ---------------------------------------------------------------------------
# Blocked / protected intervals for the in-viewer "what was recorded" summary
# (captured-events summary, U1).
#
#   blocked_intervals    — capture-time EXCLUDE-only spans (the honest "provably
#                          not captured" reassurance line).
#   protected_intervals  — the full SCRUB_BLOCK_ACTIONS set (EXCLUDE + MASK/etc.
#                          + fail-closed residuals) the digest suppresses so a
#                          masked app's window title is never surfaced.
#
# Privacy-bearing (it decides what is shown as "not captured" vs. suppressed) →
# @pytest.mark.privacy, and Vision-free (mocks the derivation, no Apple Vision)
# so CI's privacy lane runs it. The partition logic is tested directly on
# ``_read_inspect_blocked_intervals``; a separate wiring test proves the envelope
# carries the fields.
# ---------------------------------------------------------------------------

from screencap.privacy.actions import PrivacyAction  # noqa: E402
from screencap.privacy.reasons import ReasonCode  # noqa: E402
from screencap.review import _read_inspect_blocked_intervals  # noqa: E402
from screencap.scrubber import BlockedInterval, ScrubContext  # noqa: E402

_W_START = 1_716_800_000.0
_W_DUR = 60.0


def _iv(start_off: float, end_off: float, action: PrivacyAction, reason: str) -> BlockedInterval:
    return BlockedInterval(
        start=_W_START + start_off, end=_W_START + end_off, action=action, reason=reason,
    )


def _ms(off: float) -> int:
    return int(round((_W_START + off) * 1000))


def _patch_derivation(*, canonical: list, skip: list):
    """Patch the three derivation seams ``_read_inspect_blocked_intervals`` calls.

    ``canonical`` is the actioned ``build_scrub_context`` set (partitioned into
    the EXCLUDE line); ``skip`` is the full ``derive_skip_intervals`` set (the
    digest suppression set).
    """
    return (
        mock.patch(
            "screencap.backfill.skip_intervals.build_classifier_evaluator",
            return_value=(object(), object()),
        ),
        mock.patch(
            "screencap.scrubber.build_scrub_context",
            return_value=ScrubContext(blocked_intervals=canonical),
        ),
        mock.patch(
            "screencap.backfill.skip_intervals.derive_skip_intervals",
            return_value=skip,
        ),
    )


@pytest.mark.privacy
def test_blocked_line_is_exclude_only_digest_skips_full_set(recordings_root):
    """The EXCLUDE span is the only entry in the 'not captured' line; a masked
    app is suppressed from the digest (protected_intervals) but never labelled
    'not captured' — the excluded-only decision from the plan/doc-review fork."""
    rec_dir = _make_recording(recordings_root, "rec-part")
    excl = _iv(10, 20, PrivacyAction.EXCLUDE, "app_excluded")
    mask = _iv(30, 40, PrivacyAction.MASK_WINDOW, "masked")

    p1, p2, p3 = _patch_derivation(canonical=[excl, mask], skip=[excl, mask])
    with p1, p2, p3:
        blocked, protected = _read_inspect_blocked_intervals(rec_dir, _W_START, _W_DUR)

    assert blocked == [{"start_ms": _ms(10), "end_ms": _ms(20)}]
    assert protected == [
        {"start_ms": _ms(10), "end_ms": _ms(20)},
        {"start_ms": _ms(30), "end_ms": _ms(40)},
    ]


@pytest.mark.privacy
def test_secure_field_excluded_from_blocked_line(recordings_root):
    """A secure-field span is EXCLUDE-tagged but the screenshot IS captured (only
    keystrokes are nulled), so it must not appear in the 'not captured' line —
    though it is still suppressed from the digest via protected_intervals."""
    rec_dir = _make_recording(recordings_root, "rec-secure")
    sf = _iv(10, 20, PrivacyAction.EXCLUDE, ReasonCode.SECURE_FIELD_DETECTED)

    p1, p2, p3 = _patch_derivation(canonical=[sf], skip=[sf])
    with p1, p2, p3:
        blocked, protected = _read_inspect_blocked_intervals(rec_dir, _W_START, _W_DUR)

    assert blocked == []
    assert protected == [{"start_ms": _ms(10), "end_ms": _ms(20)}]


@pytest.mark.privacy
def test_no_exclusion_yields_empty_blocked_intervals(recordings_root):
    """A recording with nothing blocked yields empty (not null/missing) lists."""
    rec_dir = _make_recording(recordings_root, "rec-clean")
    p1, p2, p3 = _patch_derivation(canonical=[], skip=[])
    with p1, p2, p3:
        blocked, protected = _read_inspect_blocked_intervals(rec_dir, _W_START, _W_DUR)

    assert blocked == []
    assert protected == []


@pytest.mark.privacy
def test_overlapping_excluded_spans_collapse(recordings_root):
    """Overlapping EXCLUDE spans collapse into one interval so the 'not captured'
    total-span count is not double-counted."""
    rec_dir = _make_recording(recordings_root, "rec-overlap")
    a = _iv(10, 25, PrivacyAction.EXCLUDE, "app_excluded")
    b = _iv(20, 40, PrivacyAction.EXCLUDE, "app_excluded")

    p1, p2, p3 = _patch_derivation(canonical=[a, b], skip=[a, b])
    with p1, p2, p3:
        blocked, _ = _read_inspect_blocked_intervals(rec_dir, _W_START, _W_DUR)

    assert blocked == [{"start_ms": _ms(10), "end_ms": _ms(40)}]


@pytest.mark.privacy
def test_nullable_timing_yields_empty_intervals(recordings_root):
    """A video-only / event-free recording (null timing) has no resolvable window,
    so both lists are empty and the derivation is never attempted."""
    rec_dir = _make_recording(recordings_root, "rec-notiming")
    with mock.patch("screencap.scrubber.build_scrub_context") as bsc:
        blocked, protected = _read_inspect_blocked_intervals(rec_dir, None, None)

    assert blocked == []
    assert protected == []
    bsc.assert_not_called()


@pytest.mark.privacy
def test_blocked_derivation_failure_is_fail_open(recordings_root):
    """Any derivation error yields empty lists (fail-open read surface) — a bad
    recording.db must never fail the inspect surface."""
    rec_dir = _make_recording(recordings_root, "rec-derivefail")
    with mock.patch(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        side_effect=RuntimeError("boom"),
    ):
        blocked, protected = _read_inspect_blocked_intervals(rec_dir, _W_START, _W_DUR)

    assert blocked == []
    assert protected == []


def test_envelope_carries_blocked_and_protected_intervals(recordings_root):
    """Wiring: prepare_inspect_data surfaces both interval fields at schema 4,
    additively next to the existing inspect envelope."""
    rec_dir = _make_recording(recordings_root, "rec-env")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    fake = ([{"start_ms": 1000, "end_ms": 2000}], [{"start_ms": 1000, "end_ms": 3000}])
    with mock.patch("screencap.review._read_inspect_blocked_intervals", return_value=fake):
        env = prepare_inspect_data("rec-env")

    assert env["schema_version"] == REVIEW_SCHEMA_VERSION == 4
    assert env["blocked_intervals"] == [{"start_ms": 1000, "end_ms": 2000}]
    assert env["protected_intervals"] == [{"start_ms": 1000, "end_ms": 3000}]
