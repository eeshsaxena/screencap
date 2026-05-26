"""Tests for the daemon's per-call audit log.

The audit log is the forensic surface paired with the same-EUID trust
boundary documented in ``SECURITY.md``. It records ``recording.start``
and ``recording.stop`` invocations. Writes are best-effort: a failure
to open or write the file MUST surface as a logger.warning, never a
raised exception that breaks the underlying verb.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import audit_log, errors, provenance
from screencap.daemon.app import build_app


@pytest.fixture
def audit_log_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the audit log to a tmp path so tests don't write to ~/.screencap."""
    target = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    return target


def test_record_verb_writes_json_line_with_expected_fields(audit_log_at: Path) -> None:
    audit_log.record_verb(
        "recording.start",
        peer_pid=12345,
        peer_path="/usr/local/bin/screencap",
        classification="cli",
        outcome="ok",
        recording_name="demo",
    )

    contents = audit_log_at.read_text(encoding="utf-8").splitlines()
    assert len(contents) == 1
    record = json.loads(contents[0])
    assert record["verb"] == "recording.start"
    assert record["peer_pid"] == 12345
    assert record["peer_path"] == "/usr/local/bin/screencap"
    assert record["classification"] == "cli"
    assert record["outcome"] == "ok"
    assert record["recording_name"] == "demo"
    # Timestamp is ISO 8601 with timezone — must parse without error.
    assert "ts" in record
    assert "T" in record["ts"]


def test_record_verb_creates_file_at_0600_on_first_write(audit_log_at: Path) -> None:
    assert not audit_log_at.exists()
    audit_log.record_verb(
        "recording.start",
        peer_pid=1,
        peer_path=None,
        classification="unknown",
        outcome="ok",
    )
    assert audit_log_at.exists()
    assert stat.S_IMODE(audit_log_at.stat().st_mode) == 0o600


def test_record_verb_appends_across_calls(audit_log_at: Path) -> None:
    """Two invocations produce two lines — no truncation race."""
    for outcome in ("ok", "lock_contended"):
        audit_log.record_verb(
            "recording.start",
            peer_pid=42,
            peer_path="/x",
            classification="cli",
            outcome=outcome,
        )

    lines = audit_log_at.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["outcome"] == "ok"
    assert json.loads(lines[1])["outcome"] == "lock_contended"


def test_record_verb_handles_none_peer_info(audit_log_at: Path) -> None:
    """When provenance can't classify, the line is still written with
    ``classification=unknown`` and ``peer_pid=null``."""
    audit_log.record_verb(
        "recording.start",
        peer_pid=None,
        peer_path=None,
        classification="unknown",
        outcome="ok",
    )

    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["peer_pid"] is None
    assert record["peer_path"] is None
    assert record["classification"] == "unknown"


def test_record_verb_swallows_open_errors_via_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the audit log parent dir is unwritable, the verb must not raise.
    A warning is logged so operators can spot the regression."""
    # Point the audit-log path at a dir we make read-only.
    sealed_dir = tmp_path / "sealed"
    sealed_dir.mkdir()
    target = sealed_dir / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    os.chmod(sealed_dir, 0o500)
    try:
        with caplog.at_level(logging.WARNING, logger="screencap.daemon.audit_log"):
            audit_log.record_verb(
                "recording.start",
                peer_pid=1,
                peer_path=None,
                classification="cli",
                outcome="ok",
            )
    finally:
        os.chmod(sealed_dir, 0o700)

    assert not target.exists()
    assert any(
        "audit_log" in rec.name and rec.levelno >= logging.WARNING
        for rec in caplog.records
    )


def test_record_verb_swallows_serialization_error(
    audit_log_at: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-serializable value in ``extra`` (e.g., ``Path``) must not
    propagate out of ``record_verb``. The write is skipped and a warning
    is logged so operators can spot the regression."""
    with caplog.at_level(logging.WARNING, logger="screencap.daemon.audit_log"):
        audit_log.record_verb(
            "recording.start",
            peer_pid=1,
            peer_path=None,
            classification="cli",
            outcome="ok",
            bad=Path("/x"),
        )

    # The audit file must not have been opened: nothing wrote to it.
    assert not audit_log_at.exists()
    assert any(
        "audit_log" in rec.name and rec.levelno >= logging.WARNING
        for rec in caplog.records
    )


def test_record_verb_tightens_loose_file_mode_on_write(
    audit_log_at: Path,
) -> None:
    """Defense in depth: if an audit file already exists at a relaxed mode
    (same-UID could create it that way), record_verb tightens it to 0o600
    on the next write."""
    audit_log_at.write_text("{}\n", encoding="utf-8")
    os.chmod(audit_log_at, 0o644)

    audit_log.record_verb(
        "recording.start",
        peer_pid=1,
        peer_path=None,
        classification="cli",
        outcome="ok",
    )

    assert stat.S_IMODE(audit_log_at.stat().st_mode) == 0o600


# -- Route integration tests ------------------------------------------------
#
# The tests below exercise the wiring from the recording.start / recording.stop
# route handlers through to ``record_verb``. They monkeypatch the peer
# descriptor (no real UNIX socket) and the supervisor (no real recording).


class _RecordingSupervisor:
    """Supervisor stand-in whose spawn/stop behavior is parameterised."""

    def __init__(
        self,
        *,
        spawn_raises: BaseException | None = None,
        stop_raises: BaseException | None = None,
    ) -> None:
        self.spawn_raises = spawn_raises
        self.stop_raises = stop_raises

    def is_recovering(self) -> bool:
        return False

    async def spawn(self, parsed: object) -> dict[str, Any]:
        if self.spawn_raises is not None:
            raise self.spawn_raises
        return {"session_id": "s", "started_at": 1.0, "engine_pid": 1, "cursor": 0}

    async def stop(self, **_kwargs: Any) -> dict[str, Any]:
        if self.stop_raises is not None:
            raise self.stop_raises
        return {"session_id": "s", "stopped_at": 2.0, "cursor": 1}

    async def shutdown(self) -> None:
        return None

    def current_session(self) -> None:
        return None


def _patch_peer(monkeypatch: pytest.MonkeyPatch, descriptor: provenance.PeerDescriptor) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: descriptor,
    )


@pytest.mark.asyncio
async def test_recording_start_emits_ok_audit_line(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_peer(
        monkeypatch,
        provenance.PeerDescriptor(
            pid=4242,
            path="/Applications/ScreenCap.app/Contents/MacOS/screencap",
            classification=provenance.STARTED_BY_SWIFTUI,
        ),
    )
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.start", json={"name": "demo"})

    assert response.status_code == 200, response.text
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "recording.start"
    assert record["outcome"] == "ok"
    assert record["peer_pid"] == 4242
    assert record["classification"] == provenance.STARTED_BY_SWIFTUI
    assert record["recording_name"] == "demo"


@pytest.mark.asyncio
async def test_recording_stop_emits_ok_audit_line(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_peer(
        monkeypatch,
        provenance.PeerDescriptor(
            pid=99,
            path="/usr/local/bin/screencap",
            classification=provenance.STARTED_BY_CLI,
        ),
    )
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.stop", json={})

    assert response.status_code == 200, response.text
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "recording.stop"
    assert record["outcome"] == "ok"
    assert record["peer_pid"] == 99
    assert record["classification"] == provenance.STARTED_BY_CLI


@pytest.mark.asyncio
async def test_recording_start_audit_line_carries_typed_error_code(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed DaemonAPIError surfaces its lowercase error_code as the
    audit ``outcome`` so operators can correlate audit lines with the
    wire envelope."""
    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    contended = errors.LockContendedError(
        {"claimant": "other", "pid": 17},
        schema_version=1,
    )
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor(spawn_raises=contended)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.start", json={"name": "demo"})

    assert response.status_code == 409
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["outcome"] == errors.LOCK_CONTENDED


@pytest.mark.asyncio
async def test_recording_start_audit_line_marks_unhandled_internal_error(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor(spawn_raises=RuntimeError("boom"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.start", json={"name": "demo"})

    assert response.status_code == 500
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["outcome"] == errors.ERROR_CODE_INTERNAL


@pytest.mark.asyncio
async def test_recording_stop_audit_line_marks_unhandled_internal_error(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric to recording.start: an unhandled exception from the
    supervisor's ``stop()`` must still emit a single audit line with
    ``outcome == internal_error``."""
    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor(stop_raises=RuntimeError("boom"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.stop", json={})

    assert response.status_code == 500
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "recording.stop"
    assert record["outcome"] == errors.ERROR_CODE_INTERNAL


@pytest.mark.asyncio
async def test_recording_start_audit_line_when_peer_info_unavailable(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provenance failure (peer unreachable) still produces an audit line
    with classification=unknown and peer_pid=null."""
    _patch_peer(
        monkeypatch,
        provenance.PeerDescriptor(pid=None, path=None, classification=provenance.STARTED_BY_UNKNOWN),
    )
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.start", json={"name": "demo"})

    assert response.status_code == 200
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["peer_pid"] is None
    assert record["peer_path"] is None
    assert record["classification"] == provenance.STARTED_BY_UNKNOWN


@pytest.mark.asyncio
async def test_read_only_verbs_do_not_emit_audit_lines(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit coverage is scoped to privileged verbs. recording.list,
    session.snapshot, daemon.info, and events must not leak entries into
    the log."""
    from screencap.daemon.app import events_stream
    from starlette.datastructures import QueryParams

    class _StreamRequest:
        def __init__(self, app) -> None:
            self.app = app
            self.query_params = QueryParams("")

        async def is_disconnected(self) -> bool:
            return False

    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r1 = await client.get("/v0/daemon.info")
            r2 = await client.get("/v0/recording.list")
            r3 = await client.get("/v0/session.snapshot")
            # ``/v0/events`` is an SSE stream that runs indefinitely — invoke
            # the handler directly with a minimal request stub (same pattern
            # used by ``tests/daemon/test_event_stream.py``) and immediately
            # close its iterator. We only care that hitting the route did not
            # emit an audit line.
            events_response = await events_stream(_StreamRequest(app))
            body_iter = getattr(events_response, "body_iterator", None)
            if body_iter is not None:
                await body_iter.aclose()
            assert events_response.status_code == 200

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert not audit_log_at.exists()


@pytest.mark.asyncio
async def test_recording_stop_audit_line_carries_typed_error_code(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recording.stop's typed-error audit branch is symmetric to start's
    and must also surface the lowercase error_code as the outcome."""
    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    not_owned = errors.NotOwnedByDaemonError(claimant="other-cli", schema_version=1)
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor(stop_raises=not_owned)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.stop", json={})

    assert response.status_code == 409
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "recording.stop"
    assert record["outcome"] == errors.NOT_OWNED_BY_DAEMON


@pytest.mark.asyncio
async def test_invalid_name_audit_line_captures_rejected_name(
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forensic value: when path-traversal validation rejects a caller name,
    the audit line must still record what was rejected — recording_name
    is captured before the validator runs."""
    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _RecordingSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/v0/recording.start", json={"name": "../escape"}
            )

    assert response.status_code == 400
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["outcome"] == errors.INVALID_NAME
    assert record["recording_name"] == "../escape"


@pytest.mark.asyncio
async def test_audit_write_failure_does_not_fail_recording_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the audit log is unwritable, recording.start must still return
    success — the audit log is best-effort, never a control-plane gate.
    A warning must fire so operators can notice the regression end-to-end."""
    sealed_dir = tmp_path / "sealed"
    sealed_dir.mkdir()
    target = sealed_dir / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    os.chmod(sealed_dir, 0o500)

    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    app = build_app()
    try:
        with caplog.at_level(logging.WARNING, logger="screencap.daemon.audit_log"):
            async with app.router.lifespan_context(app):
                app.state.supervisor = _RecordingSupervisor()
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post("/v0/recording.start", json={"name": "demo"})
    finally:
        os.chmod(sealed_dir, 0o700)

    assert response.status_code == 200, response.text
    assert not target.exists()
    assert any(
        "audit_log" in rec.name and rec.levelno >= logging.WARNING
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_audit_write_failure_does_not_fail_recording_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Symmetric to start: if the audit log is unwritable, recording.stop
    must still return success and emit a warning. The audit log is
    best-effort, never a control-plane gate."""
    sealed_dir = tmp_path / "sealed"
    sealed_dir.mkdir()
    target = sealed_dir / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    os.chmod(sealed_dir, 0o500)

    _patch_peer(monkeypatch, provenance.PeerDescriptor(pid=1, path=None, classification="cli"))
    app = build_app()
    try:
        with caplog.at_level(logging.WARNING, logger="screencap.daemon.audit_log"):
            async with app.router.lifespan_context(app):
                app.state.supervisor = _RecordingSupervisor()
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post("/v0/recording.stop", json={})
    finally:
        os.chmod(sealed_dir, 0o700)

    assert response.status_code == 200, response.text
    assert not target.exists()
    assert any(
        "audit_log" in rec.name and rec.levelno >= logging.WARNING
        for rec in caplog.records
    )
