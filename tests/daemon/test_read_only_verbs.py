"""Read-only daemon verb contract tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

import screencap
from screencap.daemon import errors, schema


def _make_recording(base: Path, name: str) -> Path:
    """Create a minimal real recording catalog entry."""
    from screencap.engine.db import create_db, crud

    recording_dir = base / name
    recording_dir.mkdir(parents=True)
    db_path = recording_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    started = 1778198400.0
    recording = crud.insert_recording(
        session,
        {
            "timestamp": started,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    crud.insert_action_event(
        session,
        recording,
        started + 125.0,
        {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 200.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        },
    )
    session.close()
    engine.dispose()

    (recording_dir / "chunk_0000.mp4").write_bytes(b"fake-video")
    return recording_dir


@pytest.fixture
def isolated_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from screencap import pidfile

    lock_dir = tmp_path / "run"
    monkeypatch.setattr(pidfile, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(pidfile, "LOCK_FILE", lock_dir / "recording.lock")
    monkeypatch.setattr(pidfile, "PID_FILE", tmp_path / "recording.pid")
    pidfile._reset_for_tests()
    yield pidfile
    pidfile._reset_for_tests()


async def _asgi_get(path: str) -> httpx.Response:
    from screencap.daemon.app import build_app

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _assert_envelope(payload: dict[str, Any], *, expected_schema_version: int) -> None:
    assert payload["ok"] is True
    assert payload["schema_version"] == expected_schema_version
    assert payload["daemon_version"] == screencap.__version__
    assert payload["api_schema_version"] == schema.API_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_recording_list_matches_cli_json_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording(recordings_dir, "demo")
    (recordings_dir / "not-a-recording").mkdir()
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    from screencap.cli import cli

    cli_result = CliRunner().invoke(cli, ["--no-update-check", "list", "--json"])
    assert cli_result.exit_code == 0, cli_result.output
    expected_recordings = json.loads(cli_result.output)

    response = await _asgi_get("/v0/recording.list")

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._LIST_API_VERSION)
    assert payload["recordings"] == expected_recordings
    assert set(payload["recordings"][0]) == set(expected_recordings[0])


@pytest.mark.asyncio
async def test_session_snapshot_no_recording_keeps_all_keys(isolated_lock) -> None:
    response = await _asgi_get("/v0/session.snapshot")

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._SNAPSHOT_API_VERSION)
    assert payload["is_recording"] is False
    assert payload["daemon_owned"] is False
    assert payload["recording_name"] is None
    assert payload["started_at"] is None
    assert payload["claimant"] is None
    assert payload["recovering"] is False
    assert isinstance(payload["cursor"], int)
    assert {
        "ok",
        "schema_version",
        "daemon_version",
        "api_schema_version",
        "is_recording",
        "daemon_owned",
        "recording_name",
        "started_at",
        "claimant",
        "recovering",
        "cursor",
    } <= set(payload)


@pytest.mark.asyncio
async def test_session_snapshot_cli_claimed_lock_exposes_cas_identity(
    tmp_path: Path,
    isolated_lock,
) -> None:
    started_at = 1778198450.25
    isolated_lock.claim_lock(tmp_path / "demo", claimant="cli")
    isolated_lock.update_lock_metadata(
        tmp_path / "demo",
        recording_started_at=started_at,
        recording_name="demo",
    )

    response = await _asgi_get("/v0/session.snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_recording"] is True
    assert payload["daemon_owned"] is False
    assert payload["recording_name"] == "demo"
    assert payload["started_at"] == started_at
    assert payload["claimant"] is None
    metadata = isolated_lock.read_lock_metadata()
    assert payload["claimant_pid"] == metadata["pid"]
    assert payload["claimant_started_at"] == metadata["started_at"]
    assert "pid" not in payload
    assert "proxy_pid" not in payload
    assert "worker_pid" not in payload


@pytest.mark.asyncio
async def test_daemon_info_has_version_build_and_started_at() -> None:
    response = await _asgi_get("/v0/daemon.info")

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._DAEMON_INFO_API_VERSION)
    assert payload["daemon_version"] == screencap.__version__
    assert payload["api_schema_version"] == schema.API_SCHEMA_VERSION
    assert payload["build"] is None or isinstance(payload["build"], str)
    assert isinstance(payload["started_at"], float)
    assert payload["started_at"] <= time.time()


@pytest.mark.asyncio
async def test_session_snapshot_stale_lock_reports_not_recording(
    tmp_path: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_lock.LOCK_DIR.mkdir(parents=True)
    isolated_lock.LOCK_FILE.write_text(
        json.dumps(
            {
                "claimant": "cli",
                "recording_name": "stale",
                "recording_started_at": 1778198450.25,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(isolated_lock, "lock_is_active", lambda: False)

    response = await _asgi_get("/v0/session.snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_recording"] is False
    assert payload["daemon_owned"] is False
    assert payload["recording_name"] is None
    assert payload["started_at"] is None
    assert payload["claimant"] is None
    assert payload["recovering"] is False


@pytest.mark.asyncio
async def test_session_snapshot_consistently_inconsistent_state_is_transient(
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolated_lock, "lock_is_active", lambda: True)
    monkeypatch.setattr(isolated_lock, "read_lock_metadata", lambda: None)

    response = await _asgi_get("/v0/session.snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_recording"] is None
    assert payload["daemon_owned"] is False
    assert payload["recording_name"] is None
    assert payload["started_at"] is None
    assert payload["claimant"] is None
    assert payload["recovering"] is False


@pytest.mark.asyncio
async def test_session_snapshot_transient_metadata_race_recovers(
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_reads = iter(
        [
            None,
            {
                "claimant": "daemon",
                "recording_name": "daemon-demo",
                "recording_started_at": 1778198460.5,
                "proxy_pid": 123,
                "worker_pid": 456,
            },
        ]
    )
    monkeypatch.setattr(isolated_lock, "lock_is_active", lambda: True)
    monkeypatch.setattr(isolated_lock, "read_lock_metadata", lambda: next(metadata_reads))

    response = await _asgi_get("/v0/session.snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_recording"] is True
    assert payload["daemon_owned"] is True
    assert payload["recording_name"] == "daemon-demo"
    assert payload["started_at"] == 1778198460.5
    assert payload["claimant"] == "daemon"
    assert payload["recovering"] is False
    assert "proxy_pid" not in payload
    assert "worker_pid" not in payload


@pytest.mark.asyncio
async def test_recording_list_catalog_unreadable_returns_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap import catalog

    def unreadable() -> list[Any]:
        raise PermissionError("nope")

    monkeypatch.setattr(catalog, "list_recordings", unreadable)

    response = await _asgi_get("/v0/recording.list")

    assert response.status_code == 500
    payload = response.json()
    assert payload["ok"] is False
    assert payload["schema_version"] == schema._LIST_API_VERSION
    assert payload["daemon_version"] == screencap.__version__
    assert payload["api_schema_version"] == schema.API_SCHEMA_VERSION
    assert payload["error"] == errors.CATALOG_UNREADABLE
    assert payload["reason"]


@pytest.mark.asyncio
async def test_read_only_verbs_round_trip_over_unix_socket(
    serve_process,
    uds_client_factory,
) -> None:
    async with uds_client_factory() as client:
        info = (await client.get("/v0/daemon.info")).json()
        recordings = (await client.get("/v0/recording.list")).json()
        snapshot = (await client.get("/v0/session.snapshot")).json()

    assert serve_process.poll() is None
    assert schema.DaemonInfoResponse(**info).model_dump()["ok"] is True
    assert schema.ListResponse(**recordings).model_dump()["ok"] is True
    assert schema.SessionSnapshotResponse(**snapshot).model_dump()["ok"] is True


@pytest.mark.asyncio
async def test_recording_list_ignores_active_cli_lock(
    tmp_path: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording(recordings_dir, "while-cli-records")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    isolated_lock.claim_lock(tmp_path / "active-cli", claimant="cli")

    response = await _asgi_get("/v0/recording.list")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert [row["name"] for row in payload["recordings"]] == ["while-cli-records"]
