"""Daemon API schema envelope contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import screencap
from screencap.daemon import errors, schema


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("API_SCHEMA_VERSION", 1),
        ("_DAEMON_INFO_API_VERSION", 1),
        ("_LIST_API_VERSION", 1),
        ("_SNAPSHOT_API_VERSION", 1),
        ("_EVENTS_API_VERSION", 1),
        ("_RECORDING_START_API_VERSION", 1),
        ("_RECORDING_STOP_API_VERSION", 1),
    ],
)
def test_api_version_constants_are_pinned(name: str, expected: int) -> None:
    assert getattr(schema, name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LOCK_CONTENDED", "lock_contended"),
        ("NOT_OWNED_BY_DAEMON", "not_owned_by_daemon"),
        ("SCHEMA_MISMATCH", "schema_mismatch"),
        ("SLOW_CONSUMER", "slow_consumer"),
        ("CURSOR_UNKNOWN", "cursor_unknown"),
        ("CATALOG_UNREADABLE", "catalog_unreadable"),
        ("ROGUE_FILE", "rogue_file"),
        ("RECONCILING", "reconciling"),
        ("FORCE_MISMATCH", "force_mismatch"),
    ],
)
def test_error_code_constants_are_named_strings(name: str, expected: str) -> None:
    assert getattr(errors, name) == expected


def test_envelope_includes_uniform_keys_by_default() -> None:
    payload = schema.envelope(schema_version=schema._DAEMON_INFO_API_VERSION)

    assert payload == {
        "ok": True,
        "schema_version": schema._DAEMON_INFO_API_VERSION,
        "daemon_version": screencap.__version__,
        "api_schema_version": schema.API_SCHEMA_VERSION,
    }


def test_daemon_version_matches_package_version() -> None:
    assert schema.daemon_version() == screencap.__version__
    assert schema.daemon_version()


def test_lock_contended_error_envelope_carries_owner_verbatim() -> None:
    owner = {
        "pid": 1234,
        "claimant": "cli",
        "capture_dir": "/tmp/capture",
        "recording_started_at": 1710000000.0,
        "recording_name": "demo",
    }

    payload = errors.lock_contended_envelope(
        owner,
        schema_version=schema._RECORDING_START_API_VERSION,
    )

    assert payload["ok"] is False
    assert payload["error"] == errors.LOCK_CONTENDED
    assert payload["owner"] is owner
    assert payload["schema_version"] == schema._RECORDING_START_API_VERSION
    assert payload["daemon_version"] == screencap.__version__
    assert payload["api_schema_version"] == schema.API_SCHEMA_VERSION


def test_all_none_snapshot_fields_keep_symmetric_envelope_shape() -> None:
    payload = schema.envelope(
        schema_version=schema._SNAPSHOT_API_VERSION,
        is_recording=None,
        daemon_owned=False,
        recording_name=None,
        started_at=None,
        claimant=None,
        cursor=0,
    )
    model = schema.SessionSnapshotResponse(**payload)
    dumped = model.model_dump()

    assert dumped["ok"] is True
    assert dumped["schema_version"] == schema._SNAPSHOT_API_VERSION
    assert dumped["daemon_version"] == screencap.__version__
    assert dumped["api_schema_version"] == schema.API_SCHEMA_VERSION
    assert dumped["is_recording"] is None
    assert dumped["recording_name"] is None
    assert dumped["started_at"] is None
    assert dumped["claimant"] is None
    assert dumped["cursor"] == 0


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (
            schema.DaemonInfoResponse,
            {
                "schema_version": schema._DAEMON_INFO_API_VERSION,
                "build": None,
                "started_at": 1778198400.0,
            },
        ),
        (
            schema.ListResponse,
            {
                "schema_version": schema._LIST_API_VERSION,
                "recordings": [],
            },
        ),
        (
            schema.RecordingStartResponse,
            {
                "schema_version": schema._RECORDING_START_API_VERSION,
                "session_id": "session-1",
                "started_at": 1778198400.0,
                "engine_pid": 12345,
                "cursor": 7,
            },
        ),
        (
            schema.RecordingStopResponse,
            {
                "schema_version": schema._RECORDING_STOP_API_VERSION,
                "stopped": True,
                "final_state": "stopped",
            },
        ),
        (
            schema.RecordingMuteResponse,
            {
                "schema_version": schema._RECORDING_MUTE_API_VERSION,
                "muted": True,
                "cursor": 3,
            },
        ),
    ],
)
def test_response_models_serialize_with_envelope_keys(model_type: type, payload: dict) -> None:
    model = model_type(**schema.envelope(**payload))
    dumped = model.model_dump()

    assert dumped["ok"] is True
    assert dumped["schema_version"] == payload["schema_version"]
    assert dumped["daemon_version"] == screencap.__version__
    assert dumped["api_schema_version"] == schema.API_SCHEMA_VERSION


def test_daemon_api_error_subclass_maps_to_symmetric_error_envelope() -> None:
    exc = errors.NotOwnedByDaemonError(
        claimant="cli",
        schema_version=schema._RECORDING_STOP_API_VERSION,
    )

    payload = exc.envelope()

    assert exc.http_status == 409
    assert payload["ok"] is False
    assert payload["error"] == errors.NOT_OWNED_BY_DAEMON
    assert payload["claimant"] == "cli"
    assert payload["schema_version"] == schema._RECORDING_STOP_API_VERSION
    assert payload["daemon_version"] == screencap.__version__
    assert payload["api_schema_version"] == schema.API_SCHEMA_VERSION
    assert (
        errors.EXCEPTION_TO_ERROR_CODE[errors.NotOwnedByDaemonError]
        == errors.NOT_OWNED_BY_DAEMON
    )


def test_reconciling_error_envelope_is_retryable_503_shape() -> None:
    exc = errors.ReconcilingError(schema_version=schema._RECORDING_START_API_VERSION)

    payload = exc.envelope()

    assert exc.http_status == 503
    assert payload["ok"] is False
    assert payload["error"] == errors.RECONCILING
    assert payload["hint"] == "wait for reconciliation to complete"
    assert payload["schema_version"] == schema._RECORDING_START_API_VERSION


def test_force_mismatch_error_omits_actual_owner_values() -> None:
    payload = errors.force_mismatch_envelope(
        schema_version=schema._RECORDING_STOP_API_VERSION
    )

    assert payload["ok"] is False
    assert payload["error"] == errors.FORCE_MISMATCH
    assert "claimant_pid" not in payload
    assert "started_at" not in payload


def test_recording_stop_request_carries_force_cas_fields() -> None:
    parsed = schema.RecordingStopRequest(
        force=True,
        expected_claimant_pid=1234,
        expected_started_at=1778198400.25,
    )

    assert parsed.force is True
    assert parsed.expected_claimant_pid == 1234
    assert parsed.expected_started_at == 1778198400.25


def test_chat_answer_request_window_ms_bounds_are_enforced() -> None:
    """FIX F: ``ChatAnswerRequest.window_ms`` elements are bounded like
    ``timestamp_ms`` (ge=0, le=year-9999 epoch ms) so a malformed/huge window is a
    typed validation error at the daemon boundary, not an unbounded scan driver."""
    from pydantic import ValidationError

    # A valid in-range window parses.
    ok = schema.ChatAnswerRequest(question="q", window_ms=(1_000, 2_000))
    assert ok.window_ms == (1_000, 2_000)

    # A negative element is rejected.
    with pytest.raises(ValidationError):
        schema.ChatAnswerRequest(question="q", window_ms=(-1, 2_000))

    # An absurdly huge element (beyond the year-9999 epoch ceiling) is rejected.
    with pytest.raises(ValidationError):
        schema.ChatAnswerRequest(question="q", window_ms=(0, 10**18))

    # None (a point question) is still accepted.
    assert schema.ChatAnswerRequest(question="q").window_ms is None


def test_pydantic_models_round_trip_through_json_without_information_loss() -> None:
    recording = schema.RecordingSummary(
        name="demo",
        date="2026-05-08",
        duration="1m 02s",
        size_mb="4.0 MB",
        has_audio=True,
        transcribed=False,
        uploaded=True,
        drops={"frame": 2},
        is_stub=False,
        chunks_total=3,
        chunks_uploaded=2,
        intent="cloud",
        started_at=1778198400.0,
        duration_seconds=62.5,
    )
    snapshot = schema.SessionSnapshotResponse(
        **schema.envelope(
            schema_version=schema._SNAPSHOT_API_VERSION,
            is_recording=True,
            daemon_owned=True,
            recording_name="demo",
            started_at=1778198400.0,
            claimant="daemon",
            cursor=42,
        )
    )

    recording_dump = recording.model_dump()
    snapshot_dump = snapshot.model_dump()

    assert json.loads(json.dumps(recording_dump)) == recording_dump
    assert json.loads(json.dumps(snapshot_dump)) == snapshot_dump


def test_swiftui_style_decoder_can_tolerate_unknown_envelope_fields() -> None:
    wire = json.dumps({
        **schema.envelope(
            schema_version=schema._SNAPSHOT_API_VERSION,
            is_recording=False,
            daemon_owned=False,
            recording_name=None,
            started_at=None,
            claimant=None,
            cursor=7,
        ),
        "future_field": {"ignored": True},
    })

    decoded: dict[str, Any] = json.loads(wire)

    assert decoded["ok"] is True
    assert decoded["schema_version"] == schema._SNAPSHOT_API_VERSION
    assert decoded["daemon_version"] == screencap.__version__
    assert decoded["api_schema_version"] == schema.API_SCHEMA_VERSION


def test_public_daemon_import_does_not_import_pydantic() -> None:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "before=set(sys.modules); "
                "from screencap import daemon; "
                "after=set(sys.modules); "
                "new=after-before; "
                "assert not any(m == 'pydantic' or m.startswith('pydantic.') for m in new), "
                "sorted(m for m in new if m.startswith('pydantic'))"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=True,
    )
    assert proc.stderr == ""


def test_error_helpers_import_without_pydantic() -> None:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "before=set(sys.modules); "
                "from screencap.daemon import errors; "
                "payload=errors.slow_consumer_envelope(schema_version=1); "
                "after=set(sys.modules); "
                "new=after-before; "
                "assert payload['ok'] is False; "
                "assert payload['error'] == 'slow_consumer'; "
                "assert not any(m == 'pydantic' or m.startswith('pydantic.') for m in new), "
                "sorted(m for m in new if m.startswith('pydantic'))"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=True,
    )
    assert proc.stderr == ""
