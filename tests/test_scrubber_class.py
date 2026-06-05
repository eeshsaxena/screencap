"""Behavior tests for the Scrubber class — single-source-of-truth orchestration.

These tests target the public interface of ``screencap.scrubber.Scrubber``:
given a recording directory, calling ``run()`` (post-hoc) or ``run_chunk()``
(per-chunk) produces a privacy-scrubbed directory. No internal pipeline
functions are mocked — assertions look at directory state.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline_and_anonymizer():
    """Real detection pipeline + anonymizer (shared across module for speed)."""
    from screencap.privacy import Anonymizer, create_default_pipeline

    return create_default_pipeline(), Anonymizer()


def _make_recording_with_db_pii(rec: Path, *, secret: str) -> None:
    """Create a minimal recording.db with one PII string in action_event."""
    rec.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(rec / "recording.db"))) as db:
        db.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
        db.execute("INSERT INTO recording VALUES (1, ?)", (f"Task: contact {secret}",))
        db.execute(
            """CREATE TABLE action_event (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                name TEXT,
                timestamp REAL,
                key_char TEXT,
                canonical_key_char TEXT,
                key_name TEXT,
                canonical_key_name TEXT,
                element_state TEXT,
                active_segment_description TEXT,
                available_segment_descriptions TEXT
            )"""
        )
        db.commit()


def _write_events_jsonl(rec: Path, *, key_type_text: str) -> None:
    """Write a minimal events.jsonl with a single key.type event."""
    meta = {"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-05-04T00:00:00"}
    event = {"timestamp": 100.0, "type": "key.type", "text": key_type_text, "children": []}
    (rec / "events.jsonl").write_text(json.dumps(meta) + "\n" + json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Tracer: Scrubber.run() scrubs PII in recording.db
# ---------------------------------------------------------------------------


def test_run_scrubs_pii_in_recording_db(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() removes a known PII string from recording.task_description."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    secret = "john.doe@example.com"
    _make_recording_with_db_pii(rec, secret=secret)

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    with contextlib.closing(sqlite3.connect(str(rec / "recording.db"))) as db:
        row = db.execute("SELECT task_description FROM recording").fetchone()
    assert secret not in row[0]


def test_run_scrubs_pii_in_events_jsonl(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() removes PII from a key.type event text in events.jsonl."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    secret = "alice@contoso.example"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    _write_events_jsonl(rec, key_type_text=f"my email is {secret}")

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    body = (rec / "events.jsonl").read_text()
    assert secret not in body


def test_run_scrubs_pii_in_transcripts(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() removes PII from transcript_*.txt and transcript_*.json."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    secret_email = "alice@contoso.example"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    (rec / "transcript.txt").write_text(f"Contact us at {secret_email} for details.")
    transcript = {
        "text": f"Contact {secret_email}",
        "segments": [{"text": f"Contact {secret_email}", "start": 0.0, "end": 1.0}],
        "words": [
            {"word": "Contact", "start": 0.0, "end": 0.3},
            {"word": secret_email, "start": 0.3, "end": 1.0},
        ],
    }
    (rec / "transcript.json").write_text(json.dumps(transcript))

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    assert secret_email not in (rec / "transcript.txt").read_text()
    assert secret_email not in (rec / "transcript.json").read_text()


def test_run_redacts_hostname_and_wifi_in_metrics(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() rule-redacts hostname + wifi.ssid/bssid in system_metrics.json."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    metrics = {
        "static": {
            "hostname": "alices-laptop",
            "wifi": {"ssid": "HomeNet", "bssid": "AA:BB:CC:DD:EE:FF"},
        },
    }
    (rec / "system_metrics.json").write_text(json.dumps(metrics))

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run()

    scrubbed = json.loads((rec / "system_metrics.json").read_text())
    assert scrubbed["static"]["hostname"] == "<REDACTED>"
    assert scrubbed["static"]["wifi"]["ssid"] == "<REDACTED>"
    assert scrubbed["static"]["wifi"]["bssid"] == "<REDACTED>"


def _make_recording_with_blocked_interval(
    rec: Path, *, blocked_bundle_id: str, blocked_start: float, blocked_end: float,
) -> None:
    """Create recording.db with window switching to a blocked app for one interval.

    action_event rows: one before the interval (keeps content), one inside
    (must be nulled), one after (keeps content). Window events frame the
    interval with a benign editor app on either side.
    """
    rec.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(rec / "recording.db"))) as db:
        db.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        db.execute("INSERT INTO recording VALUES (1, 0.0, 2.0)")
        db.execute(
            """CREATE TABLE action_event (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                name TEXT,
                timestamp REAL,
                key_char TEXT,
                canonical_key_char TEXT,
                key_name TEXT,
                canonical_key_name TEXT,
                element_state TEXT,
                active_segment_description TEXT,
                available_segment_descriptions TEXT
            )"""
        )
        for row_id, ts, ch in [
            (1, blocked_start - 5, "a"),
            (2, (blocked_start + blocked_end) / 2, "s"),
            (3, blocked_end + 5, "d"),
        ]:
            db.execute(
                "INSERT INTO action_event VALUES "
                "(?, 1, 'press', ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (row_id, ts, ch, ch, ch, ch),
            )
        db.execute(
            """CREATE TABLE window_event (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                timestamp REAL,
                app_bundle_id TEXT,
                window_id TEXT,
                title TEXT,
                state TEXT,
                browser_url TEXT,
                secure_input INTEGER
            )"""
        )
        db.execute(
            "INSERT INTO window_event VALUES "
            "(1, 1, ?, 'com.microsoft.VSCode', 'w-vscode', 'Editor', NULL, NULL, 0)",
            (blocked_start - 10,),
        )
        db.execute(
            "INSERT INTO window_event VALUES "
            "(2, 1, ?, ?, 'w-blocked', 'Vault', NULL, NULL, 0)",
            (blocked_start, blocked_bundle_id),
        )
        db.execute(
            "INSERT INTO window_event VALUES "
            "(3, 1, ?, 'com.microsoft.VSCode', 'w-vscode', 'Editor', NULL, NULL, 0)",
            (blocked_end,),
        )
        db.commit()


def test_run_nulls_action_rows_inside_blocked_interval(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run() nulls action_event content inside an excluded-app interval."""
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    blocked_start, blocked_end = 100.0, 200.0
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=blocked_start,
        blocked_end=blocked_end,
    )

    # Internal mode keeps the surrounding editor windows ALLOW so we can
    # cleanly observe the EXCLUDE interval around the password manager.
    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run()

    with contextlib.closing(sqlite3.connect(str(rec / "recording.db"))) as db:
        rows = {
            row[0]: row[1]
            for row in db.execute("SELECT id, key_char FROM action_event").fetchall()
        }
    assert rows[1] == "a", "row before interval should be preserved"
    assert rows[2] is None, "row inside blocked interval should be nulled"
    assert rows[3] == "d", "row after interval should be preserved"


def test_run_writes_audit_log_when_blocked_intervals_present(
    tmp_path, pipeline_and_anonymizer,
):
    """privacy_audit.json is written when blocked-interval audit entries exist."""
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=100.0,
        blocked_end=200.0,
    )

    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run()

    audit_path = rec / "privacy_audit.json"
    assert audit_path.exists()
    data = json.loads(audit_path.read_text())
    assert any(e.get("surface") == "db_field" for e in data["entries"])


# ---------------------------------------------------------------------------
# run_chunk(): per-chunk scrubbing
# ---------------------------------------------------------------------------


def _write_chunk_events_jsonl(rec: Path, idx: int, *, key_type_text: str) -> Path:
    """Write a minimal events_NNNN.jsonl with one key.type event."""
    meta = {"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-05-04T00:00:00"}
    event = {"timestamp": 100.0, "type": "key.type", "text": key_type_text, "children": []}
    path = rec / f"events_{idx:04d}.jsonl"
    path.write_text(json.dumps(meta) + "\n" + json.dumps(event) + "\n")
    return path


def test_run_chunk_scrubs_events_file_for_index(tmp_path, pipeline_and_anonymizer):
    """Scrubber.run_chunk() scrubs only the events_NNNN.jsonl for the given index."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    secret = "carol@contoso.example"
    _write_chunk_events_jsonl(rec, idx=2, key_type_text=f"reach me at {secret}")
    other_secret = "untouched@contoso.example"
    other = _write_chunk_events_jsonl(rec, idx=5, key_type_text=f"do not scrub {other_secret}")

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run_chunk(
        idx=2, start_ts=0.0, end_ts=1000.0, transcript_path=None,
    )

    assert secret not in (rec / "events_0002.jsonl").read_text()
    assert other_secret in other.read_text(), "other-index events file must be untouched"


def test_run_chunk_scrubs_transcripts_and_v1_manifest(tmp_path, pipeline_and_anonymizer):
    """run_chunk() scrubs the chunk's transcript files and v1 manifest task titles."""
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")

    secret_t = "dave@contoso.example"
    transcript_txt = rec / "transcript_0003.txt"
    transcript_txt.write_text(f"Reach {secret_t} for sales.")
    transcript_json = rec / "transcript_0003.json"
    transcript_json.write_text(json.dumps({"text": f"Reach {secret_t}"}))

    secret_m = "erin@contoso.example"
    manifest = {
        "format_version": 1,
        "tasks": [
            {
                "dominant_title": f"Compose to {secret_m}",
                "dominant_app": "com.apple.mail",
                "start_ts": 0.0,
                "end_ts": 100.0,
            }
        ],
    }
    manifest_path = rec / "chunk_0003_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    from screencap.scrubber import Scrubber

    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run_chunk(
        idx=3, start_ts=0.0, end_ts=1000.0, transcript_path=transcript_txt,
    )

    assert secret_t not in transcript_txt.read_text()
    assert secret_t not in transcript_json.read_text()
    assert secret_m not in manifest_path.read_text()


def test_run_chunk_deletes_excluded_app_screenshots(tmp_path, pipeline_and_anonymizer):
    """run_chunk() removes screenshots taken while an excluded app is frontmost."""
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    blocked_start, blocked_end = 100.0, 200.0
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=blocked_start,
        blocked_end=blocked_end,
    )

    # Place screenshots in chunk_4/screenshots/ — one inside the blocked
    # interval (must be deleted), one outside (must remain). The
    # associate_screenshot lookup uses a 5s window, so place each shot
    # within 5s of the window event it should associate with.
    shot_dir = rec / "chunk_4" / "screenshots"
    shot_dir.mkdir(parents=True)
    inside = shot_dir / f"{blocked_start + 1.0}.jpg"
    outside = shot_dir / f"{blocked_start - 9.0 + 1.0}.jpg"  # 1s after VSCode at t-10
    inside.write_bytes(b"\x00")
    outside.write_bytes(b"\x00")

    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run_chunk(idx=4, start_ts=0.0, end_ts=300.0, transcript_path=None)

    assert not inside.exists(), "screenshot inside excluded interval should be deleted"
    assert outside.exists(), "screenshot outside excluded interval should remain"


# ---------------------------------------------------------------------------
# scrub_recording() CLI entry-point: copy + delegate + summarize
# ---------------------------------------------------------------------------


def test_scrub_recording_copies_and_scrubs_pii(tmp_path):
    """``scrub_recording`` copies the source dir to ``<name>-scrubbed`` and scrubs PII."""
    from unittest import mock

    name = "test-recording"
    src = tmp_path / name
    secret = "frank@contoso.example"
    _make_recording_with_db_pii(src, secret=secret)

    from screencap.scrubber import scrub_recording

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path,
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path,
    ):
        result = scrub_recording(name)

    dst = tmp_path / f"{name}-scrubbed"
    assert dst.exists()
    assert src.exists(), "source recording must remain untouched"
    with contextlib.closing(sqlite3.connect(str(dst / "recording.db"))) as db:
        row = db.execute("SELECT task_description FROM recording").fetchone()
    assert secret not in row[0]
    assert result.output_dir == dst


def test_scrub_recording_raises_when_recording_missing(tmp_path):
    """``scrub_recording`` raises FileNotFoundError when the recording does not exist."""
    from unittest import mock

    from screencap.scrubber import scrub_recording

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path,
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path,
    ), pytest.raises(FileNotFoundError):
        scrub_recording("does-not-exist")


# ---------------------------------------------------------------------------
# Pointer-geometry suppression — load-bearing CLAUDE.md invariant
# ---------------------------------------------------------------------------
#
# CLAUDE.md ("Scrubbing pipeline") states ``scrub_events_jsonl`` MUST drop
# ``mouse.move`` events whose timestamp falls inside an interval whose
# privacy action is in ``SCRUB_BLOCK_ACTIONS``. The set is broader than
# ``BLOCK_ACTIONS`` — TEXT_REDACT covers code editors, OCR_FALLBACK covers
# unverified browsers — because pointer geometry inside content-sensitive
# contexts is comparably sensitive (which terminal line was being edited,
# which credentials field was being hovered).
#
# These tests guard the cloud-bound privacy posture: a regression that
# stopped dropping ``mouse.move`` would silently leak pointer geometry to
# GCS. Recovery via ``screencap upload`` only protects the recovered
# JSONL when this drop runs, so the invariant is also tested at the
# recovery boundary in ``test_recover_chunk_metadata.py``.


def _write_events_with_mouse_move(
    rec: Path, *, before_ts: float, in_interval_ts: float, after_ts: float,
) -> None:
    """Write events.jsonl with one mouse.move at each given timestamp."""
    rec.mkdir(parents=True, exist_ok=True)
    meta = {"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-05-04T00:00:00"}
    events = [
        {"timestamp": before_ts, "type": "mouse.move", "mouse_x": 10.0, "mouse_y": 10.0},
        {"timestamp": in_interval_ts, "type": "mouse.move", "mouse_x": 100.0, "mouse_y": 100.0},
        {"timestamp": after_ts, "type": "mouse.move", "mouse_x": 200.0, "mouse_y": 200.0},
    ]
    lines = [json.dumps(meta)] + [json.dumps(e) for e in events]
    (rec / "events.jsonl").write_text("\n".join(lines) + "\n")


def test_run_drops_mouse_move_inside_excluded_app_interval(
    tmp_path, pipeline_and_anonymizer,
):
    """``mouse.move`` events inside an EXCLUDE interval must be dropped.

    Pins the load-bearing CLAUDE.md "Scrubbing pipeline" invariant: when
    a mouse.move's timestamp lands inside an interval whose privacy
    action is in ``SCRUB_BLOCK_ACTIONS``, the event is dropped from
    ``events.jsonl``. Without this drop, pointer geometry inside a
    password-manager (or code editor / admin console / unverified
    browser) leaks to GCS even when content is otherwise scrubbed.
    """
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    blocked_start, blocked_end = 100.0, 200.0
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=blocked_start,
        blocked_end=blocked_end,
    )
    in_interval_ts = (blocked_start + blocked_end) / 2  # 150
    after_ts = blocked_end + 50  # 250
    # Mouse moves: one inside the EXCLUDE interval, one after — clearly
    # in a non-blocked window context.
    _write_events_with_mouse_move(
        rec,
        before_ts=in_interval_ts,  # not used, asserted-on
        in_interval_ts=in_interval_ts,
        after_ts=after_ts,
    )

    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run()

    out_lines = (rec / "events.jsonl").read_text().splitlines()
    moves_kept = [
        json.loads(line)["timestamp"]
        for line in out_lines
        if line.strip() and not json.loads(line).get("_meta")
        and json.loads(line).get("type") == "mouse.move"
    ]
    assert in_interval_ts not in moves_kept, (
        "mouse.move inside an EXCLUDE interval must be dropped (CLAUDE.md "
        "SCRUB_BLOCK_ACTIONS pointer-suppression invariant)"
    )
    assert after_ts in moves_kept, (
        f"mouse.move outside any blocked interval must be preserved; "
        f"moves_kept={moves_kept}"
    )


# ---------------------------------------------------------------------------
# Robustness to malformed inputs
# ---------------------------------------------------------------------------


def test_run_chunk_tolerates_malformed_jsonl_line(tmp_path, pipeline_and_anonymizer):
    """A corrupt JSONL line must not abort the chunk scrub.

    The file is renamed to ``.scrub_failed`` to mark it ineligible for
    upload (fail-closed at the file boundary), but the call returns
    normally so the rest of the chunk still gets scrubbed.
    """
    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")

    meta = {"_meta": True, "screencap_version": "0.1.0", "exported_at": "2026-05-04T00:00:00"}
    good = {"timestamp": 100.0, "type": "mouse.move", "mouse_x": 1.0, "mouse_y": 2.0}
    events_path = rec / "events_0001.jsonl"
    events_path.write_text(
        json.dumps(meta) + "\n"
        + "{this is not valid json at all\n"
        + json.dumps(good) + "\n",
    )

    from screencap.scrubber import Scrubber

    # Must not raise; the chunk completes despite the corrupt line.
    Scrubber(rec, pipeline=pipeline, anonymizer=anonymizer).run_chunk(
        idx=1, start_ts=0.0, end_ts=200.0, transcript_path=None,
    )

    failed_path = events_path.with_suffix(events_path.suffix + ".scrub_failed")
    assert failed_path.exists(), (
        "fail-closed: the file with the corrupt line must be renamed to "
        f".scrub_failed so it never gets uploaded; rec contents="
        f"{[p.name for p in rec.iterdir()]}"
    )


def test_scrub_recording_rejects_path_traversal(tmp_path):
    """``scrub_recording`` refuses names that escape the recordings dir."""
    from unittest import mock

    from screencap.scrubber import scrub_recording

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path,
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path,
    ), pytest.raises(ValueError):
        scrub_recording("../etc/passwd")


# ---------------------------------------------------------------------------
# U1: redaction evidence on ScrubResult + audit JSON (R8/R13/R14)
# ---------------------------------------------------------------------------


def test_run_threads_blocked_intervals_onto_result_and_audit(
    tmp_path, pipeline_and_anonymizer,
):
    """A blocked-app interval surfaces on ``result.blocked_intervals`` and in
    ``privacy_audit.json`` with start/end/reason (R13 evidence)."""
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator, parse_privacy_config

    pipeline, anonymizer = pipeline_and_anonymizer
    rec = tmp_path / "rec"
    _make_recording_with_blocked_interval(
        rec,
        blocked_bundle_id="com.1password.1password",
        blocked_start=100.0,
        blocked_end=200.0,
    )

    cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier()

    from screencap.scrubber import Scrubber

    result = Scrubber(
        rec,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=evaluator,
        classifier=classifier,
    ).run()

    assert result.blocked_intervals, "blocked interval must be threaded onto the result"
    iv = result.blocked_intervals[0]
    assert iv.start == 100.0
    assert iv.end == 200.0

    data = json.loads((rec / "privacy_audit.json").read_text())
    assert data["blocked_intervals"], "audit JSON must carry the blocked-interval list"
    entry = data["blocked_intervals"][0]
    assert set(entry) == {"start", "end", "action", "reason"}
    assert entry["start"] == 100.0
    assert entry["end"] == 200.0
    assert entry["reason"], "reason code must be present"


def test_write_audit_log_persists_blocked_intervals_without_audit_entries(tmp_path):
    """Guard-fix regression: blocked intervals are written even with zero
    per-decision audit entries.

    The old ``if not result.audit_entries: return`` guard dropped the
    blocked-interval list whenever NER found nothing. Tested at the writer
    because the full ``run()`` path co-produces a ``db_field`` entry per
    interval — the writer is where the dropped-evidence bug actually lived.
    """
    from screencap.privacy.actions import PrivacyAction
    from screencap.scrubber import BlockedInterval, ScrubResult, _write_audit_log

    result = ScrubResult()
    result.blocked_intervals = [
        BlockedInterval(
            start=10.0, end=20.0,
            action=PrivacyAction.EXCLUDE, reason="blocked_app_exclude",
        )
    ]

    _write_audit_log(tmp_path, result)

    data = json.loads((tmp_path / "privacy_audit.json").read_text())
    assert data["entries"] == []
    assert len(data["blocked_intervals"]) == 1
    assert data["blocked_intervals"][0]["start"] == 10.0
    assert data["blocked_intervals"][0]["end"] == 20.0


def test_write_audit_log_emits_empty_collections_not_missing_keys(tmp_path):
    """No blocked intervals / no fail-closed → keys present as empty lists."""
    from screencap.privacy.reasons import AuditEntry
    from screencap.scrubber import ScrubResult, _write_audit_log

    result = ScrubResult()
    result.audit_entries = [
        AuditEntry(
            timestamp=1.0, surface="screenshot",
            action="exclude", reason="policy_excluded_app",
        )
    ]

    _write_audit_log(tmp_path, result)

    data = json.loads((tmp_path / "privacy_audit.json").read_text())
    assert data["blocked_intervals"] == [], "empty collection, not a missing key"
    assert data["fail_closed"] == [], "empty collection, not a missing key"
    assert data["schema_version"] == 1


def test_write_audit_log_serializes_open_interval_end_as_null(tmp_path):
    """An open-ended (``inf``) interval serializes ``end`` as JSON null.

    ``json.dumps`` would otherwise emit invalid ``Infinity``, which the
    Swift consumer's ``JSONDecoder`` rejects.
    """
    from screencap.privacy.actions import PrivacyAction
    from screencap.scrubber import BlockedInterval, ScrubResult, _write_audit_log

    result = ScrubResult()
    result.blocked_intervals = [
        BlockedInterval(
            start=5.0, end=float("inf"),
            action=PrivacyAction.MASK_WINDOW, reason="blocked_app_mask",
        )
    ]

    _write_audit_log(tmp_path, result)

    raw = (tmp_path / "privacy_audit.json").read_text()
    assert "Infinity" not in raw, "inf must not leak as invalid JSON"
    data = json.loads(raw)
    assert data["blocked_intervals"][0]["end"] is None


def test_run_records_fail_closed_when_detectors_fail(tmp_path):
    """A field the scrubber can't analyze trips ``<SCRUB_FAILED>`` and is
    recorded in ``fail_closed_redactions`` with a timestamp (Covers AE8).

    The raw value never appears in the result or the audit JSON.
    """
    from screencap.privacy import AllDetectorsFailedError
    from screencap.scrubber import SCRUB_FAILED_SENTINEL, Scrubber

    rec = tmp_path / "rec"
    secret = "topsecret-passphrase-9000"
    _make_recording_with_db_pii(rec, secret="placeholder@example.com")
    _write_events_jsonl(rec, key_type_text=secret)  # timestamp 100.0

    class _AllFailPipeline:
        def detect(self, text):
            raise AllDetectorsFailedError("forced detector failure for test")

    class _StubAnonymizer:
        def anonymize(self, text, detections):  # never reached on failure
            return text

    result = Scrubber(
        rec, pipeline=_AllFailPipeline(), anonymizer=_StubAnonymizer(),
    ).run()

    assert result.fail_closed_redactions, "fail-closed field must be recorded"
    marker = result.fail_closed_redactions[0]
    assert marker["timestamp"] == 100.0
    assert marker["surface"] == "event"

    body = (rec / "events.jsonl").read_text()
    assert secret not in body, "raw value must never survive a fail-closed scrub"
    assert SCRUB_FAILED_SENTINEL in body, "the field was removed to be safe"

    data = json.loads((rec / "privacy_audit.json").read_text())
    assert any(m["timestamp"] == 100.0 for m in data["fail_closed"])
    assert secret not in json.dumps(data), "raw value must never reach the audit JSON"


def test_scrub_recording_creates_scrubbed_dir_mode_0700(tmp_path):
    """``scrub_recording`` locks the scrubbed copy to owner-only (0700)."""
    import stat as _stat
    from unittest import mock

    name = "modes-rec"
    src = tmp_path / name
    _make_recording_with_db_pii(src, secret="grace@contoso.example")

    from screencap.scrubber import scrub_recording

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path,
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path,
    ):
        scrub_recording(name)

    dst = tmp_path / f"{name}-scrubbed"
    assert _stat.S_IMODE(dst.stat().st_mode) == 0o700


def test_write_audit_log_writes_file_mode_0600(tmp_path):
    """``privacy_audit.json`` is written owner-read/write only (0600)."""
    import stat as _stat

    from screencap.privacy.reasons import AuditEntry
    from screencap.scrubber import ScrubResult, _write_audit_log

    result = ScrubResult()
    result.audit_entries = [
        AuditEntry(
            timestamp=1.0, surface="screenshot",
            action="exclude", reason="policy_excluded_app",
        )
    ]

    _write_audit_log(tmp_path, result)

    audit = tmp_path / "privacy_audit.json"
    assert _stat.S_IMODE(audit.stat().st_mode) == 0o600
