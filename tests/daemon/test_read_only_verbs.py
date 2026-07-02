"""Read-only daemon verb contract tests."""

from __future__ import annotations

import asyncio
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


@pytest.mark.real_permission_probe
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
    # U2: the additive grant block rides the real fresh-subprocess probe through
    # daemon.info (no mock here — integration coverage of the live path).
    assert set(payload["permissions"]) == {
        "screen_recording",
        "accessibility",
        "input_monitoring",
    }
    for value in payload["permissions"].values():
        assert value in {"granted", "denied", "indeterminate"}
    # Envelope still validates against the additive model.
    schema.DaemonInfoResponse(**payload)


def _stub_probe(monkeypatch: pytest.MonkeyPatch, grants: dict[str, str]) -> None:
    from screencap.daemon import permission_probe

    monkeypatch.setattr(permission_probe, "probe_permissions", lambda: dict(grants))


@pytest.mark.asyncio
async def test_daemon_info_reports_all_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(
        monkeypatch,
        {"screen_recording": "granted", "accessibility": "granted", "input_monitoring": "granted"},
    )
    payload = (await _asgi_get("/v0/daemon.info")).json()
    assert payload["permissions"] == {
        "screen_recording": "granted",
        "accessibility": "granted",
        "input_monitoring": "granted",
    }


@pytest.mark.asyncio
async def test_daemon_info_reports_one_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(
        monkeypatch,
        {"screen_recording": "denied", "accessibility": "granted", "input_monitoring": "granted"},
    )
    payload = (await _asgi_get("/v0/daemon.info")).json()
    assert payload["permissions"]["screen_recording"] == "denied"
    assert payload["permissions"]["accessibility"] == "granted"
    assert payload["permissions"]["input_monitoring"] == "granted"


@pytest.mark.asyncio
async def test_daemon_info_reports_indeterminate_not_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An indeterminate probe is reported as indeterminate — present, not
    omitted, and never coerced to a falsy/denied value."""
    _stub_probe(
        monkeypatch,
        {
            "screen_recording": "indeterminate",
            "accessibility": "indeterminate",
            "input_monitoring": "indeterminate",
        },
    )
    payload = (await _asgi_get("/v0/daemon.info")).json()
    assert payload["permissions"] == {
        "screen_recording": "indeterminate",
        "accessibility": "indeterminate",
        "input_monitoring": "indeterminate",
    }


@pytest.mark.asyncio
async def test_daemon_info_concurrent_calls_trigger_single_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-flight guard coalesces concurrent daemon.info probes into a single
    subprocess spawn (fork-bomb guard)."""
    from screencap.daemon import permission_probe
    from screencap.daemon.app import build_app

    calls: list[int] = []

    def _counting_probe() -> dict[str, str]:
        calls.append(1)
        return {
            "screen_recording": "granted",
            "accessibility": "granted",
            "input_monitoring": "granted",
        }

    monkeypatch.setattr(permission_probe, "probe_permissions", _counting_probe)

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1, r2 = await asyncio.gather(
            client.get("/v0/daemon.info"),
            client.get("/v0/daemon.info"),
        )

    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 1, "concurrent daemon.info must not fan out probe spawns"
    assert r1.json()["permissions"] == r2.json()["permissions"]


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
async def test_session_snapshot_lock_held_without_recording_started_at_is_idle(
    tmp_path: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SessionController and other long-lived holders claim the lock with
    # metadata but no per-recording timestamp between recordings. The
    # daemon must mirror the pidfile.py:276-279 invariant and report
    # is_recording=false in that case — otherwise SwiftUI shows a
    # phantom "another process is recording" banner during those gaps.
    isolated_lock.LOCK_DIR.mkdir(parents=True)
    isolated_lock.LOCK_FILE.write_text(
        json.dumps(
            {
                "claimant": "cli",
                "recording_name": None,
                # recording_started_at intentionally omitted
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(isolated_lock, "lock_is_active", lambda: True)

    response = await _asgi_get("/v0/session.snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_recording"] is False
    assert payload["daemon_owned"] is False
    assert payload["recording_name"] is None
    assert payload["started_at"] is None
    assert payload["claimant"] is None


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


# --- SCR-148: cloud account-mismatch observability surface ---


@pytest.mark.asyncio
async def test_recording_list_surfaces_owner_uid_and_upload_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recording.list exposes the pinned owner uid + deferred-upload warning."""
    from screencap.catalog import write_owner_uid

    recordings_dir = tmp_path / "recordings"
    rec = _make_recording(recordings_dir, "owned")
    write_owner_uid(rec, "uid-abc")
    (rec / ".upload_followup.json").write_text(json.dumps({
        "kind": "upload_disabled",
        "n_uploaded": 0,
        "n_total": 1,
        "upload_warning": "uploads disabled: offline",
    }))
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_get("/v0/recording.list")
    assert response.status_code == 200
    payload = response.json()
    summary = payload["recordings"][0]
    assert summary["owner_uid"] == "uid-abc"
    assert summary["upload_warning"] == "uploads disabled: offline"


@pytest.mark.asyncio
async def test_auth_whoami_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """/v0/auth.whoami reports the signed-in uid/email in the daemon envelope."""
    from screencap import auth

    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "uid": "uid-xyz", "email": "a@b.com"},
    )
    response = await _asgi_get("/v0/auth.whoami")
    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._AUTH_WHOAMI_API_VERSION)
    assert payload["signed_in"] is True
    assert payload["uid"] == "uid-xyz"
    assert payload["email"] == "a@b.com"
    assert payload["stale"] is False


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_auth_whoami_forwards_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """KTD7 "extended whoami": the daemon envelope carries the plan tier."""
    from screencap import auth

    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "uid": "u", "email": "e@b.com", "plan": "founding"},
    )
    response = await _asgi_get("/v0/auth.whoami")
    assert response.status_code == 200
    payload = response.json()
    assert payload["plan"] == "founding"


# --------------------------------------------------------------------------
# U3 — /v0/auth.entitlements read verb
# --------------------------------------------------------------------------


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_auth_entitlements_founding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A founding account reads back active with a null (serialized) expires."""
    from screencap import auth

    monkeypatch.setattr(
        auth, "get_entitlements",
        lambda: {"plan": "founding", "active": True, "expires": None},
    )
    response = await _asgi_get("/v0/auth.entitlements")
    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._AUTH_ENTITLEMENTS_API_VERSION)
    assert payload["plan"] == "founding"
    assert payload["active"] is True
    # Nullable field serialized as JSON null, NOT omitted (the Swift decoder
    # must see the contract field — the nullable-JSON-contract learning).
    assert "expires" in payload
    assert payload["expires"] is None


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_auth_entitlements_signed_out_free(monkeypatch: pytest.MonkeyPatch) -> None:
    from screencap import auth

    monkeypatch.setattr(
        auth, "get_entitlements",
        lambda: {"plan": "free", "active": False, "expires": None},
    )
    response = await _asgi_get("/v0/auth.entitlements")
    assert response.status_code == 200
    payload = response.json()
    assert payload["plan"] == "free"
    assert payload["active"] is False


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_auth_entitlements_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A get_entitlements() raise degrades to free/inactive — never a 500."""
    from screencap import auth

    def _boom() -> dict[str, Any]:
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(auth, "get_entitlements", _boom)
    response = await _asgi_get("/v0/auth.entitlements")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["plan"] == "free"
    assert payload["active"] is False
    assert payload["expires"] is None


@pytest.mark.asyncio
async def test_auth_whoami_signed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signed-out reports signed_in=false and null uid/email (never raises)."""
    from screencap import auth

    monkeypatch.setattr(auth, "whoami", lambda: {"signed_in": False})
    response = await _asgi_get("/v0/auth.whoami")
    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._AUTH_WHOAMI_API_VERSION)
    assert payload["signed_in"] is False
    assert payload["uid"] is None
    assert payload["email"] is None


@pytest.mark.asyncio
async def test_auth_whoami_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whoami() raise degrades to signed_in=false — a read verb never 500s."""
    from screencap import auth

    def _boom() -> dict[str, Any]:
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(auth, "whoami", _boom)
    response = await _asgi_get("/v0/auth.whoami")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["signed_in"] is False


@pytest.mark.asyncio
async def test_auth_whoami_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale/offline shape: signed_in=True, uid=None, email=None, stale=True.

    This shape is produced when the token cache is populated but the Keychain
    probe cannot verify freshness (e.g. offline). The daemon must surface it
    intact so callers know to treat the account as temporarily UNVERIFIABLE
    rather than signed-out or mismatched.
    """
    from screencap import auth

    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "uid": None, "email": None, "stale": True},
    )
    response = await _asgi_get("/v0/auth.whoami")
    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._AUTH_WHOAMI_API_VERSION)
    assert payload["signed_in"] is True
    assert payload["uid"] is None
    assert payload["email"] is None
    assert payload["stale"] is True


@pytest.mark.asyncio
async def test_read_only_verbs_round_trip_over_unix_socket(
    serve_process,
    uds_client_factory,
) -> None:
    async with uds_client_factory() as client:
        info = (await client.get("/v0/daemon.info")).json()
        recordings = (await client.get("/v0/recording.list")).json()
        snapshot = (await client.get("/v0/session.snapshot")).json()
        whoami = (await client.get("/v0/auth.whoami")).json()

    assert serve_process.poll() is None
    assert schema.DaemonInfoResponse(**info).model_dump()["ok"] is True
    assert schema.ListResponse(**recordings).model_dump()["ok"] is True
    assert schema.SessionSnapshotResponse(**snapshot).model_dump()["ok"] is True
    # auth.whoami exercises the real fail-open path — signed_in may be False in
    # CI (no Keychain), but the envelope must always be well-formed.
    assert whoami["ok"] is True
    assert "signed_in" in whoami
    assert "uid" in whoami
    assert "email" in whoami
    assert "stale" in whoami


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


# ---------------------------------------------------------------------------
# SCR-118 content.search (U3)
# ---------------------------------------------------------------------------


async def _asgi_post(path: str, body: dict) -> httpx.Response:
    from screencap.daemon.app import build_app

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


def _seed_content_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Seed a content-index store and point the daemon handler at it."""
    import screencap.content_index as content_index

    store_path = tmp_path / "content_index.db"
    monkeypatch.setattr(content_index, "default_index_path", lambda: store_path)
    return content_index, store_path


@pytest.mark.asyncio
async def test_content_search_returns_pointer_only_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_index, store_path = _seed_content_index(tmp_path, monkeypatch)
    with content_index.ContentIndex(store_path) as store:
        store.write_frames(
            "demo",
            [content_index.IndexFrame(timestamp_ms=125_000, text="the invoice total was wrong")],
        )

    response = await _asgi_post("/v0/content.search", {"query": "invoice"})

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._CONTENT_SEARCH_API_VERSION)
    assert payload["index_state"] == "ok"
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    # Pointer-only: exactly these fields, no path / image bytes.
    assert set(hit) == {"recording", "timestamp_ms", "snippet", "score"}
    assert hit["recording"] == "demo"
    assert hit["timestamp_ms"] == 125_000
    assert "invoice" in hit["snippet"].lower()
    # No media-path-shaped value anywhere in the serialized body.
    assert ".mp4" not in response.text
    assert ".jpg" not in response.text


@pytest.mark.asyncio
async def test_content_search_not_indexed_when_store_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_content_index(tmp_path, monkeypatch)  # store path set but file absent

    response = await _asgi_post("/v0/content.search", {"query": "anything"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["hits"] == []
    assert payload["index_state"] == "not_indexed"
    # A read must not have created an empty PII store.
    assert not (tmp_path / "content_index.db").exists()


@pytest.mark.asyncio
async def test_content_search_rejects_traversal_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_content_index(tmp_path, monkeypatch)

    response = await _asgi_post(
        "/v0/content.search", {"query": "x", "recording": "../../etc"}
    )

    assert response.status_code >= 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_content_search_fts_injection_is_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_index, store_path = _seed_content_index(tmp_path, monkeypatch)
    with content_index.ContentIndex(store_path) as store:
        store.write_frames(
            "demo", [content_index.IndexFrame(timestamp_ms=1000, text="ordinary words")]
        )

    for query in ('"', "*", "NEAR", "recording:demo", "a AND b"):
        response = await _asgi_post("/v0/content.search", {"query": query})
        assert response.status_code == 200, query
        assert response.json()["ok"] is True


@pytest.mark.asyncio
async def test_content_search_limit_is_clamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_index, store_path = _seed_content_index(tmp_path, monkeypatch)
    with content_index.ContentIndex(store_path) as store:
        store.write_frames(
            "demo",
            [content_index.IndexFrame(timestamp_ms=i, text=f"match {i}") for i in range(30)],
        )

    response = await _asgi_post("/v0/content.search", {"query": "match", "limit": 5})
    assert response.status_code == 200
    assert len(response.json()["hits"]) == 5


# ---------------------------------------------------------------------------
# SCR-118 transcript.search + timeline.query (U4)
# ---------------------------------------------------------------------------

_REC_STARTED = 1778198400.0


def _make_recording_with_windows(base: Path, name: str, windows: list[dict]) -> Path:
    """Create a recording.db with window_event rows for timeline tests."""
    from screencap.engine.db import create_db, crud

    recording_dir = base / name
    recording_dir.mkdir(parents=True)
    engine, Session = create_db(str(recording_dir / "recording.db"))
    session = Session()
    recording = crud.insert_recording(
        session,
        {
            "timestamp": _REC_STARTED,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    for w in windows:
        crud.insert_window_event(
            session, recording, _REC_STARTED + w["offset"],
            {
                "title": w.get("title"),
                "app_name": w.get("app_name"),
                "app_bundle_id": w.get("bundle"),
                "browser_url": w.get("url"),
            },
        )
    session.close()
    engine.dispose()
    return recording_dir


def _guard_content_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the content store at a tmp path so we can assert it is never opened."""
    import screencap.content_index as content_index

    store_path = tmp_path / "content_index.db"
    monkeypatch.setattr(content_index, "default_index_path", lambda: store_path)
    return store_path


@pytest.mark.asyncio
async def test_timeline_query_returns_rows_without_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Safari", "bundle": "com.apple.Safari", "title": "Docs"},
            {"offset": 20, "app_name": "Code", "bundle": "com.microsoft.VSCode", "title": "main.py"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    content_store = _guard_content_store(tmp_path, monkeypatch)

    response = await _asgi_post("/v0/timeline.query", {})

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._TIMELINE_QUERY_API_VERSION)
    assert payload["coverage"] == "authoritative"
    apps = [r["app"] for r in payload["rows"]]
    assert apps == ["Safari", "Code"]  # wall-clock order
    assert all(set(r) == {"recording", "timestamp_ms", "app", "title"} for r in payload["rows"])
    # AE5: answered purely from event tables — the content index is never opened.
    assert not content_store.exists()


@pytest.mark.asyncio
async def test_timeline_query_app_filter_and_omits_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Safari", "bundle": "com.apple.Safari",
             "title": "Login", "url": "https://x.test/callback?code=SECRET_TOKEN"},
            {"offset": 20, "app_name": "Code", "bundle": "com.microsoft.VSCode", "title": "main.py"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/timeline.query", {"app": "safari"})

    assert response.status_code == 200
    payload = response.json()
    assert [r["app"] for r in payload["rows"]] == ["Safari"]
    # v1 omits browser_url — the OAuth code must not appear anywhere.
    assert "SECRET_TOKEN" not in response.text
    assert "url" not in payload["rows"][0]


@pytest.mark.asyncio
async def test_timeline_query_matches_browser_url_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCR-179: a site token matches a browser row via the browser_url hostname,
    even though the app is just "Safari". The URL never enters the row."""
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Safari", "bundle": "com.apple.Safari",
             "title": "Issues", "url": "https://github.com/org/repo/issues"},
            {"offset": 20, "app_name": "Code", "bundle": "com.microsoft.VSCode",
             "title": "main.py"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/timeline.query", {"app": "github"})

    assert response.status_code == 200
    payload = response.json()
    assert [r["app"] for r in payload["rows"]] == ["Safari"]
    # Structural invariant: exactly these keys — never browser_url.
    assert all(
        set(r) == {"recording", "timestamp_ms", "app", "title"} for r in payload["rows"]
    )


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_timeline_query_domain_match_never_leaks_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4: matching on browser_url must not leak the URL. The path/query (where
    OAuth codes live) appears nowhere, and the row key set is exact."""
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Safari", "bundle": "com.apple.Safari",
             "title": "Login",
             "url": "https://app.example.com/oauth?code=SECRET_TOKEN&state=XYZ"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/timeline.query", {"app": "example"})

    assert response.status_code == 200
    payload = response.json()
    assert [r["app"] for r in payload["rows"]] == ["Safari"]  # matched via host
    assert set(payload["rows"][0]) == {"recording", "timestamp_ms", "app", "title"}
    assert "SECRET_TOKEN" not in response.text
    assert "code=" not in response.text
    assert "/oauth" not in response.text


@pytest.mark.asyncio
async def test_timeline_query_domain_match_applies_limit_after_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matching browser row sorts AFTER many non-matching rows — it must
    still be returned, proving `limit` is applied after the hostname filter (the
    naive 'filter in Python after the SQL LIMIT' would drop it)."""
    recordings_dir = tmp_path / "recordings"
    windows = [
        {"offset": float(i), "app_name": "Code", "bundle": "com.microsoft.VSCode"}
        for i in range(1, 6)
    ]
    windows.append(
        {"offset": 100.0, "app_name": "Safari", "bundle": "com.apple.Safari",
         "title": "Issues", "url": "https://github.com/x"}
    )
    _make_recording_with_windows(recordings_dir, "demo", windows)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/timeline.query", {"app": "github", "limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert [r["app"] for r in payload["rows"]] == ["Safari"]


@pytest.mark.asyncio
async def test_timeline_query_unmatched_token_excludes_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Safari", "bundle": "com.apple.Safari",
             "url": "https://github.com/x"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/timeline.query", {"app": "nonesuch"})

    assert response.status_code == 200
    assert response.json()["rows"] == []


@pytest.mark.asyncio
async def test_timeline_query_domain_match_tolerates_malformed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Notes", "bundle": "com.apple.Notes",
             "url": "not a url"},
            {"offset": 20, "app_name": "Safari", "bundle": "com.apple.Safari",
             "url": "https://github.com/x"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    # The malformed-URL row yields no host (non-match, no crash); the valid one matches.
    response = await _asgi_post("/v0/timeline.query", {"app": "github"})

    assert response.status_code == 200
    assert [r["app"] for r in response.json()["rows"]] == ["Safari"]


@pytest.mark.asyncio
async def test_timeline_query_spans_recordings_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "rec-a", [{"offset": 30, "app_name": "Mail", "bundle": "com.apple.mail"}]
    )
    _make_recording_with_windows(
        recordings_dir, "rec-b", [{"offset": 10, "app_name": "Slack", "bundle": "com.slack"}]
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/timeline.query", {})

    payload = response.json()
    # rec-b's event (offset 10) is earlier in wall-clock than rec-a's (offset 30).
    assert [(r["recording"], r["app"]) for r in payload["rows"]] == [
        ("rec-b", "Slack"),
        ("rec-a", "Mail"),
    ]


@pytest.mark.asyncio
async def test_timeline_query_inverted_range_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "recordings"))
    response = await _asgi_post("/v0/timeline.query", {"start_ms": 5000, "end_ms": 1000})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_range"


@pytest.mark.asyncio
async def test_transcript_search_returns_chunk_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    rec = recordings_dir / "demo"
    rec.mkdir(parents=True)
    (rec / "transcript_0003.txt").write_text("we discussed the quarterly budget review")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/transcript.search", {"query": "budget"})

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._TRANSCRIPT_SEARCH_API_VERSION)
    assert payload["coverage"] == "best_effort"
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    # SCR-186: the hit gains nullable chunk-granular timing fields.
    assert set(hit) == {
        "recording", "chunk_index", "snippet",
        "timestamp_ms", "timestamp_granularity", "chunk_duration_ms",
    }
    assert hit["recording"] == "demo"
    assert hit["chunk_index"] == 3
    assert "budget" in hit["snippet"].lower()
    # No chunk manifest seeded -> timing is null, the hit is still returned.
    assert hit["timestamp_ms"] is None
    assert hit["timestamp_granularity"] is None
    assert hit["chunk_duration_ms"] is None


@pytest.mark.asyncio
async def test_transcript_search_never_reads_raw_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    rec = recordings_dir / "demo"
    rec.mkdir(parents=True)
    # Only the rich raw JSON exists (per-word fields = R7 leak) — never read.
    (rec / "transcript_0001.json").write_text('{"words": [{"word": "budget"}]}')
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/transcript.search", {"query": "budget"})

    assert response.status_code == 200
    assert response.json()["hits"] == []


@pytest.mark.asyncio
async def test_transcript_search_never_reads_scrub_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    rec = recordings_dir / "demo"
    rec.mkdir(parents=True)
    # The appended .scrub_failed suffix means scrubbing failed — never read.
    (rec / "transcript_0001.txt.scrub_failed").write_text("unscrubbed budget secret")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/transcript.search", {"query": "budget"})

    assert response.status_code == 200
    assert response.json()["hits"] == []


@pytest.mark.asyncio
async def test_transcript_search_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "recordings"))
    response = await _asgi_post(
        "/v0/transcript.search", {"query": "x", "recording": "../../etc"}
    )
    assert response.status_code >= 400
    assert response.json()["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_transcript_search_empty_query_returns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    rec = recordings_dir / "demo"
    rec.mkdir(parents=True)
    (rec / "transcript_0001.txt").write_text("some private transcript content")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    # An empty/whitespace query must NOT dump the whole transcript corpus.
    response = await _asgi_post("/v0/transcript.search", {"query": "   "})
    assert response.status_code == 200
    assert response.json()["hits"] == []


@pytest.mark.asyncio
async def test_timeline_query_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "recordings"))
    response = await _asgi_post("/v0/timeline.query", {"recording": "../../etc"})
    assert response.status_code >= 400
    assert response.json()["error"] == "invalid_name"


# ---------------------------------------------------------------------------
# SCR-118 query verbs are excluded from idle-shutdown activity (U5)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SCR-179 apps.list — query-parser vocabulary (U1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apps_list_enumerates_apps_bundles_and_hostnames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "rec-a",
        [
            {"offset": 10, "app_name": "Slack", "bundle": "com.tinyspeck.slackmacgap"},
            {"offset": 20, "app_name": "Safari", "bundle": "com.apple.Safari",
             "url": "https://github.com/issues/1"},
        ],
    )
    _make_recording_with_windows(
        recordings_dir, "rec-b",
        # Duplicate Slack (dedup across recordings) + a second site.
        [
            {"offset": 5, "app_name": "Slack", "bundle": "com.tinyspeck.slackmacgap"},
            {"offset": 15, "app_name": "Chrome", "bundle": "com.google.Chrome",
             "url": "https://mail.google.com/mail/u/0"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_get("/v0/apps.list")

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._APPS_LIST_API_VERSION)
    assert set(payload["app_names"]) == {"Slack", "Safari", "Chrome"}  # deduped
    assert "com.tinyspeck.slackmacgap" in payload["app_bundles"]
    assert set(payload["hostnames"]) == {"github.com", "mail.google.com"}
    assert payload["truncated"] is False
    # Envelope validates against the additive model.
    schema.AppsListResponse(**payload)


@pytest.mark.asyncio
async def test_apps_list_empty_when_no_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "recordings"))
    response = await _asgi_get("/v0/apps.list")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["app_names"] == []
    assert payload["app_bundles"] == []
    assert payload["hostnames"] == []
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_apps_list_tolerates_null_and_malformed_browser_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Code", "bundle": "com.microsoft.VSCode"},  # no url
            {"offset": 20, "app_name": "Notes", "bundle": "com.apple.Notes",
             "url": "not a url at all"},  # no extractable host
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_get("/v0/apps.list")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["app_names"]) == {"Code", "Notes"}
    # No host could be extracted — but no crash, and no garbage hostname.
    assert payload["hostnames"] == []


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_apps_list_returns_bare_hostnames_never_full_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6: a token-bearing URL contributes only its bare host. The path/query
    (where OAuth codes / session tokens live) must appear nowhere."""
    recordings_dir = tmp_path / "recordings"
    _make_recording_with_windows(
        recordings_dir, "demo",
        [
            {"offset": 10, "app_name": "Safari", "bundle": "com.apple.Safari",
             "url": "https://app.example.com/oauth?code=SECRET_TOKEN&state=XYZ"},
        ],
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_get("/v0/apps.list")

    assert response.status_code == 200
    payload = response.json()
    assert payload["hostnames"] == ["app.example.com"]
    # Structural guard: each hostname is a bare host — no path/query delimiters.
    for host in payload["hostnames"]:
        assert "/" not in host and "?" not in host and "=" not in host
    # The secret / full URL appears nowhere in the serialized body.
    assert "SECRET_TOKEN" not in response.text
    assert "code=" not in response.text
    assert "/oauth" not in response.text


@pytest.mark.asyncio
async def test_apps_list_does_not_bump_idle_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apps.list is read-only — NOT in _ACTIVITY_PATHS, so a vocabulary refresh
    must not reset the idle-shutdown clock."""
    from screencap.daemon import _idle_shutdown
    from screencap.daemon.app import build_app

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "recordings"))

    app = build_app()
    _idle_shutdown.attach(app, idle_seconds=600.0)
    sentinel = 12345.0
    app.state.idle_last_activity = sentinel

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v0/apps.list")
    assert resp.status_code == 200
    assert app.state.idle_last_activity == sentinel


@pytest.mark.asyncio
async def test_query_verbs_do_not_bump_idle_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three read verbs are NOT in ``_ACTIVITY_PATHS`` — a query must not
    reset the idle-shutdown clock (the MCP-held subscription keeps the daemon
    alive instead, so cron-style polling can't pin an auto-spawned daemon)."""
    from screencap.daemon import _idle_shutdown
    from screencap.daemon.app import build_app

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "recordings"))
    _guard_content_store(tmp_path, monkeypatch)

    app = build_app()
    _idle_shutdown.attach(app, idle_seconds=600.0)
    sentinel = 12345.0
    app.state.idle_last_activity = sentinel

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path, body in (
            ("/v0/content.search", {"query": "anything"}),
            ("/v0/transcript.search", {"query": "anything"}),
            ("/v0/timeline.query", {}),
            ("/v0/frame.nearest", {"recording": "none", "timestamp_ms": 0}),
        ):
            resp = await client.post(path, json=body)
            assert resp.status_code == 200, path
            # The activity middleware must have left the clock untouched.
            assert app.state.idle_last_activity == sentinel, path


# ---------------------------------------------------------------------------
# SCR-186 frame.nearest (U2) + transcript chunk-timing enrichment (U3)
# ---------------------------------------------------------------------------

_FRAME_T0 = 1_719_400_000.0  # epoch seconds anchor for frame fixtures


def _seed_screenshots(recordings_dir: Path, name: str, epochs: list[float]) -> Path:
    """Create ``<recordings_dir>/<name>/screenshots/{epoch:.6f}.jpg`` fixtures."""
    screenshots = recordings_dir / name / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    for e in epochs:
        (screenshots / f"{e:.6f}.jpg").write_bytes(b"")
    return recordings_dir / name


def _allow_all_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the ALLOW-only filter to allow every frame (isolates verb wiring).

    The fail-closed derivation itself is covered by tests/test_frame_blocked.py;
    here we control the predicate so frame.nearest tests exercise the verb, not
    the privacy machinery.
    """
    import screencap.frame_blocked as fb

    monkeypatch.setattr(fb, "build_is_blocked", lambda rec_dir, tss: (lambda ts: False))


@pytest.mark.asyncio
async def test_frame_nearest_resolves_within_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _seed_screenshots(recordings_dir, "demo", [_FRAME_T0, _FRAME_T0 + 10.0])
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    _allow_all_frames(monkeypatch)

    # 1s after the 2nd frame (10s mark).
    anchor_ms = int((_FRAME_T0 + 11.0) * 1000)
    response = await _asgi_post(
        "/v0/frame.nearest", {"recording": "demo", "timestamp_ms": anchor_ms}
    )

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._FRAME_NEAREST_API_VERSION)
    assert payload["stem"] == "1719400010.000000"
    assert payload["delta_ms"] == -1000  # frame is 1s before the anchor


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_frame_nearest_response_is_pointer_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _seed_screenshots(recordings_dir, "demo", [_FRAME_T0])
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    _allow_all_frames(monkeypatch)

    response = await _asgi_post(
        "/v0/frame.nearest",
        {"recording": "demo", "timestamp_ms": int(_FRAME_T0 * 1000)},
    )

    assert response.status_code == 200
    # Pointer-only: a bare stem, never a path or image bytes.
    payload = response.json()
    assert ".jpg" not in response.text
    assert "screenshots" not in response.text
    assert "/" not in payload["stem"]
    # Structural lock: exactly the envelope keys + {stem, delta_ms}, nothing else.
    assert set(payload) == {
        "ok", "schema_version", "daemon_version", "api_schema_version",
        "stem", "delta_ms",
    }


@pytest.mark.asyncio
async def test_frame_nearest_over_cap_is_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    _seed_screenshots(recordings_dir, "demo", [_FRAME_T0])
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    _allow_all_frames(monkeypatch)

    anchor_ms = int((_FRAME_T0 + 120.0) * 1000)  # 2 min away, past the 30s cap
    response = await _asgi_post(
        "/v0/frame.nearest", {"recording": "demo", "timestamp_ms": anchor_ms}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["stem"] is None
    assert payload["delta_ms"] is None


@pytest.mark.asyncio
async def test_frame_nearest_missing_screenshots_is_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    (recordings_dir / "demo").mkdir(parents=True)  # no screenshots/ dir
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post(
        "/v0/frame.nearest", {"recording": "demo", "timestamp_ms": int(_FRAME_T0 * 1000)}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["stem"] is None


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_frame_nearest_skips_blocked_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The nearest frame is blocked -> the nearest ALLOW frame is returned. R8:
    # the verb never points an agent at a masked frame even when it is closest.
    recordings_dir = tmp_path / "recordings"
    _seed_screenshots(recordings_dir, "demo", [_FRAME_T0, _FRAME_T0 + 10.0])
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    import screencap.frame_blocked as fb

    blocked_ts = _FRAME_T0 + 10.0
    monkeypatch.setattr(
        fb, "build_is_blocked",
        lambda rec_dir, tss: (lambda ts: ts == blocked_ts),
    )

    anchor_ms = int((_FRAME_T0 + 11.0) * 1000)  # closest to the blocked 10s frame
    response = await _asgi_post(
        "/v0/frame.nearest", {"recording": "demo", "timestamp_ms": anchor_ms}
    )

    assert response.status_code == 200
    assert response.json()["stem"] == "1719400000.000000"  # the ALLOW frame


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_frame_nearest_fails_closed_all_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Indeterminate blocked geometry -> every frame blocked -> miss (not a frame).
    recordings_dir = tmp_path / "recordings"
    _seed_screenshots(recordings_dir, "demo", [_FRAME_T0, _FRAME_T0 + 10.0])
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    import screencap.frame_blocked as fb

    monkeypatch.setattr(fb, "build_is_blocked", lambda rec_dir, tss: (lambda ts: True))

    response = await _asgi_post(
        "/v0/frame.nearest",
        {"recording": "demo", "timestamp_ms": int(_FRAME_T0 * 1000)},
    )

    assert response.status_code == 200
    assert response.json()["stem"] is None


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_frame_nearest_rejects_traversal_recording() -> None:
    response = await _asgi_post(
        "/v0/frame.nearest", {"recording": "../../etc", "timestamp_ms": 0}
    )
    assert response.status_code >= 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_frame_nearest_rejects_malformed_request() -> None:
    # Missing required timestamp_ms.
    r1 = await _asgi_post("/v0/frame.nearest", {"recording": "demo"})
    assert r1.status_code == 400
    assert r1.json()["error"] == "invalid_request"

    # Over-bound staleness cap (> 24h).
    r2 = await _asgi_post(
        "/v0/frame.nearest",
        {"recording": "demo", "timestamp_ms": 0, "staleness_cap_ms": 10**12},
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_request"

    # Negative timestamp.
    r3 = await _asgi_post(
        "/v0/frame.nearest", {"recording": "demo", "timestamp_ms": -1}
    )
    assert r3.status_code == 400
    assert r3.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_transcript_search_enriches_with_chunk_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recordings_dir = tmp_path / "recordings"
    rec = recordings_dir / "demo"
    rec.mkdir(parents=True)
    (rec / "transcript_0002.txt").write_text("we discussed the quarterly budget review")
    (rec / "chunk_0002_manifest.json").write_text(
        json.dumps({"chunk_index": 2, "chunk_start": _FRAME_T0, "chunk_end": _FRAME_T0 + 900.0})
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/transcript.search", {"query": "budget"})

    assert response.status_code == 200
    hit = response.json()["hits"][0]
    assert hit["chunk_index"] == 2
    assert hit["timestamp_ms"] == int(_FRAME_T0 * 1000)
    assert hit["timestamp_granularity"] == "chunk"
    assert hit["chunk_duration_ms"] == 900_000


@pytest.mark.asyncio
async def test_transcript_search_bare_txt_is_not_chunk_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bare whole-recording transcript.txt spans every chunk; it must NOT be
    # stamped with chunk-0 timing (which would mislocate a late match), even when
    # a chunk_0000 manifest exists.
    recordings_dir = tmp_path / "recordings"
    rec = recordings_dir / "demo"
    rec.mkdir(parents=True)
    (rec / "transcript.txt").write_text("the budget was discussed near the end")
    (rec / "chunk_0000_manifest.json").write_text(
        json.dumps({"chunk_index": 0, "chunk_start": _FRAME_T0, "chunk_end": _FRAME_T0 + 900.0})
    )
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

    response = await _asgi_post("/v0/transcript.search", {"query": "budget"})

    assert response.status_code == 200
    hit = response.json()["hits"][0]
    assert hit["chunk_index"] == 0  # bare file parses to 0
    assert hit["timestamp_ms"] is None  # but carries no resolvable anchor
    assert hit["timestamp_granularity"] is None
    assert hit["chunk_duration_ms"] is None


@pytest.mark.asyncio
async def test_transcript_to_frame_nearest_chunk_granular_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9: a transcript hit resolves to its chunk frame ONLY with a chunk-scaled
    cap; the 30s default deliberately misses a mid-chunk anchor."""
    recordings_dir = tmp_path / "recordings"
    rec = recordings_dir / "demo"
    rec.mkdir(parents=True)
    (rec / "transcript_0000.txt").write_text("the budget discussion happened here")
    (rec / "chunk_0000_manifest.json").write_text(
        json.dumps({"chunk_index": 0, "chunk_start": _FRAME_T0, "chunk_end": _FRAME_T0 + 900.0})
    )
    # The only frame sits 5 min into the 15-min chunk — far from chunk_start.
    _seed_screenshots(recordings_dir, "demo", [_FRAME_T0 + 300.0])
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    _allow_all_frames(monkeypatch)

    tr = await _asgi_post("/v0/transcript.search", {"query": "budget"})
    hit = tr.json()["hits"][0]
    anchor_ms = hit["timestamp_ms"]
    chunk_cap = hit["chunk_duration_ms"]

    # Chunk-scaled cap -> resolves to the chunk's frame.
    resolved = await _asgi_post(
        "/v0/frame.nearest",
        {"recording": "demo", "timestamp_ms": anchor_ms, "staleness_cap_ms": chunk_cap},
    )
    assert resolved.json()["stem"] == "1719400300.000000"

    # 30s default cap -> the mid-chunk anchor misses (the loop's whole point).
    missed = await _asgi_post(
        "/v0/frame.nearest", {"recording": "demo", "timestamp_ms": anchor_ms}
    )
    assert missed.json()["stem"] is None
