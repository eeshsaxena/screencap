"""HTTP-layer tests for the recording.rename daemon verb (editable titles U3).

Drives the handler over an in-process ASGI transport (``build_app`` +
``httpx.ASGITransport``) against real recording.db fixtures in a temp recordings
dir — the same lightweight harness the read-only-verb and audit-log tests use.
No engine, no subprocess: rename is a post-hoc verb over the local-only
``recording.db``.

Covers: set, clear, invalid title (control char + over-length), unknown
selector, legacy-null-recording_id resolution by directory name, active-recording
rejection, a unicode/emoji title, and the audit-log title-omission property.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import audit_log, errors, provenance, schema
from screencap.daemon.app import build_app

_STARTED_AT = 1778198400.0  # a fixed capture-start epoch for a stable default title


def _make_recording(base: Path, name: str, *, recording_id: str | None) -> Path:
    """Create a minimal real recording dir with a recording.db.

    When ``recording_id`` is None, no ``.recording_id`` sidecar is written — the
    legacy shape whose only stable handle is the directory name.
    """
    from screencap.engine.db import create_db, crud

    recording_dir = base / name
    recording_dir.mkdir(parents=True)
    db_path = recording_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": _STARTED_AT,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    session.close()
    engine.dispose()

    if recording_id is not None:
        (recording_dir / ".recording_id").write_text(recording_id, encoding="utf-8")
    return recording_dir


@pytest.fixture
def audit_log_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the audit log to a tmp path so tests never touch ~/.screencap."""
    target = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    return target


@pytest.fixture
def recordings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "recordings"
    d.mkdir()
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(d))
    return d


def _patch_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=4321,
            path="/usr/local/bin/screencap",
            classification=provenance.STARTED_BY_CLI,
        ),
    )


def _patch_active(monkeypatch: pytest.MonkeyPatch, name: str | None) -> None:
    """Force ``catalog._active_recording_name`` deterministic (hermetic vs. a
    real lock the dev machine might hold)."""
    from screencap import catalog

    monkeypatch.setattr(catalog, "_active_recording_name", lambda: name)


async def _post_rename(recording_id: str, title: str):
    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v0/recording.rename",
            json={"recording_id": recording_id, "title": title},
        )


def _read_title(db_path: Path) -> str | None:
    from screencap import catalog

    return catalog._read_user_title(db_path)


@pytest.mark.asyncio
async def test_rename_sets_title(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "demo", recording_id="rid-demo")

    resp = await _post_rename("rid-demo", "Quarterly Review")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["title"] == "Quarterly Review"
    assert body["title_is_user_set"] is True
    assert isinstance(body["cursor"], int)
    # Persisted and readable back through the catalog reader.
    assert _read_title(rec / "recording.db") == "Quarterly Review"


@pytest.mark.asyncio
async def test_rename_by_directory_name_when_recording_id_present(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory-name fallback resolves even when a .recording_id exists."""
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "demo", recording_id="rid-demo")

    resp = await _post_rename("demo", "By Dir Name")

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "By Dir Name"
    assert _read_title(rec / "recording.db") == "By Dir Name"


@pytest.mark.asyncio
async def test_empty_title_clears_and_returns_default(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "demo", recording_id="rid-demo")
    db = rec / "recording.db"

    # First set a title, then clear it.
    await _post_rename("rid-demo", "Temp Name")
    assert _read_title(db) == "Temp Name"

    resp = await _post_rename("rid-demo", "")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title_is_user_set"] is False
    # Cleared → the freshly-resolved date/time default, matching recording.list.
    from screencap import catalog

    assert body["title"] == catalog._default_title(_STARTED_AT, "demo")
    # The column is cleared back to NULL.
    assert _read_title(db) is None


@pytest.mark.asyncio
async def test_control_character_title_is_rejected_and_not_written(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "demo", recording_id="rid-demo")
    db = rec / "recording.db"

    resp = await _post_rename("rid-demo", "bad\x07title")

    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == errors.INVALID_NAME
    # The raw title must never be echoed back in the error envelope.
    assert "bad" not in json.dumps(body)
    # Nothing was written.
    assert _read_title(db) is None


@pytest.mark.asyncio
async def test_over_length_title_is_rejected_and_not_written(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "demo", recording_id="rid-demo")
    db = rec / "recording.db"

    resp = await _post_rename("rid-demo", "x" * 201)

    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_NAME
    assert _read_title(db) is None
    # Boundary: exactly 200 is accepted.
    ok = await _post_rename("rid-demo", "y" * 200)
    assert ok.status_code == 200, ok.text
    assert _read_title(db) == "y" * 200


@pytest.mark.asyncio
async def test_unknown_selector_returns_not_found(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    _make_recording(recordings_dir, "demo", recording_id="rid-demo")

    resp = await _post_rename("does-not-exist", "New Title")

    assert resp.status_code == 404
    assert resp.json()["error"] == errors.RECORDING_NOT_FOUND


@pytest.mark.asyncio
async def test_legacy_null_recording_id_renameable_by_directory_name(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording predating .recording_id (read_recording_id → None) is still
    renameable by its directory name."""
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "legacy-rec", recording_id=None)
    from screencap import catalog

    assert catalog.read_recording_id(rec) is None  # precondition

    resp = await _post_rename("legacy-rec", "Renamed Legacy")

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Renamed Legacy"
    assert _read_title(rec / "recording.db") == "Renamed Legacy"


@pytest.mark.asyncio
async def test_renaming_active_recording_is_rejected(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "live", recording_id="rid-live")
    db = rec / "recording.db"
    # The target directory is the currently-active recording.
    _patch_active(monkeypatch, "live")

    resp = await _post_rename("rid-live", "Should Not Apply")

    assert resp.status_code == 409
    assert resp.json()["error"] == errors.RECORDING_ACTIVE
    # Refused before any write.
    assert _read_title(db) is None


@pytest.mark.asyncio
async def test_unicode_emoji_title_is_accepted(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "demo", recording_id="rid-demo")

    title = "Café ☕ réunion 🎉 日本語"
    resp = await _post_rename("rid-demo", title)

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == title
    assert _read_title(rec / "recording.db") == title


@pytest.mark.asyncio
async def test_audit_line_omits_title_text(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit record must carry peer + outcome ONLY — never the free-text
    title (titles must not accrue in the local audit log)."""
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    _make_recording(recordings_dir, "demo", recording_id="rid-demo")

    secret_title = "SuperSecretTitleXYZ"
    resp = await _post_rename("rid-demo", secret_title)
    assert resp.status_code == 200, resp.text

    raw = audit_log_at.read_text(encoding="utf-8")
    record = json.loads(raw.splitlines()[-1])
    assert record["verb"] == "recording.rename"
    assert record["outcome"] == "ok"
    assert record["peer_pid"] == 4321
    assert record["classification"] == provenance.STARTED_BY_CLI
    # The title text appears nowhere in the audit line.
    assert secret_title not in raw
    assert "title" not in record


@pytest.mark.asyncio
async def test_format_and_separator_title_is_rejected_and_not_written(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bidi overrides / zero-width (Cf) and line separators (Zl/Zp) are rejected.

    None belong in a single-line display label — Cf enables card display spoofing
    and Zl/Zp break the layout — so they are refused like control characters, and
    nothing is written.
    """
    _patch_peer(monkeypatch)
    _patch_active(monkeypatch, None)
    rec = _make_recording(recordings_dir, "demo", recording_id="rid-demo")
    db = rec / "recording.db"

    # U+202E bidi override (Cf), U+200B zero-width (Cf), U+2028 line separator (Zl).
    for bad in ("spoof\u202etitle", "zero\u200bwidth", "line\u2028break"):
        resp = await _post_rename("rid-demo", bad)
        assert resp.status_code == 400, bad
        assert resp.json()["error"] == errors.INVALID_NAME
        assert _read_title(db) is None
