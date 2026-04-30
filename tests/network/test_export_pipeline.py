"""Tests for screencap.network.export_pipeline (V1.5 NetworkScrubPipeline).

Covers:

* :class:`NetworkScrubPipeline` round-trip: encrypted-body → export
  event with scrubbed plaintext.
* Capture-event-without-ciphertext passes through with
  ``body_text=None``.
* AAD mismatch yields ``body_text=None`` and a debug log.
* Decryption-tag failure (tampered ciphertext) yields
  ``body_text=None``.
* Unsupported event type → ``ValueError``.
* :class:`KekUnavailableError` paths: KEK Keychain raises;
  ``network_event_meta`` row missing.
* :func:`recording_has_encrypted_bodies` reflects DB state.
* All four capture event types decrypt successfully.

The tests mock ``keyring.get_password`` and ``keyring.set_password`` so
they NEVER touch the macOS Keychain.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from screencap.engine.db import create_db, crud, get_session_for_path
from screencap.engine.db.models import Recording
from screencap.engine.events import (
    NetworkRequestEvent,
    NetworkRequestExportEvent,
    NetworkResponseEvent,
    NetworkResponseExportEvent,
    NetworkWebSocketFrameEvent,
    NetworkWebSocketFrameExportEvent,
    NetworkWebSocketUpgradeEvent,
    NetworkWebSocketUpgradeExportEvent,
)
from screencap.network import crypto
from screencap.network.export_pipeline import (
    KekUnavailableError,
    NetworkScrubPipeline,
    recording_has_encrypted_bodies,
    recording_has_wrapped_dek,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_recording(tmp_path: Path) -> tuple[str, int, bytes, bytes]:
    """Create a recording.db with a Recording row and a NetworkEventMeta row.

    Returns (db_path, recording_id, kek_bytes, dek_bytes). The KEK is the
    in-memory bytes that callers should supply via mocked
    ``keyring.get_password``; the DEK is the unwrapped form available
    for direct ciphertext construction in the test.
    """
    db_path = str(tmp_path / "recording.db")
    engine, Session = create_db(db_path)
    session = Session()
    try:
        rec = crud.insert_recording(session, {
            "timestamp": 1000.0,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        })
        recording_id = rec.id

        # Generate a KEK + DEK and persist the wrapped DEK.
        kek = crypto._generate_kek()
        dek = crypto.generate_dek()
        wrapped, nonce = crypto.wrap_dek(dek, kek)
        crud.insert_network_event_meta(
            session,
            recording_id=recording_id,
            dek_wrapped=wrapped,
            dek_nonce=nonce,
        )
    finally:
        session.close()
        engine.dispose()
    return db_path, recording_id, kek, dek


def _kek_patch(kek: bytes):
    """Return a context manager that mocks keyring to surface ``kek``.

    Mocks ``keyring.get_password`` to return the base64-encoded KEK
    bytes (mirroring what ``keyring.set_password`` would have stored).
    """
    import base64

    return patch(
        "keyring.get_password",
        return_value=base64.b64encode(kek).decode("ascii"),
    )


def _make_request_event(
    *,
    recording_id: int,
    dek: bytes,
    flow_id: str = "flow-aaa",
    timestamp_ns: int = 1_000_000_000,
    body: bytes = b"hello",
) -> NetworkRequestEvent:
    """Build a capture-side NetworkRequestEvent with real ciphertext."""
    aad = crypto.aad_bytes(
        recording_id=recording_id,
        flow_id=flow_id,
        event_type="network.request",
        ts_ns=timestamp_ns,
    )
    ciphertext, nonce = crypto.encrypt_body(dek, body, aad)
    return NetworkRequestEvent(
        timestamp=timestamp_ns / 1e9,
        timestamp_ns=timestamp_ns,
        flow_id=flow_id,
        method="POST",
        url="https://example.com/api",
        host="example.com",
        headers=[],
        body_size=len(body),
        body_sha256_hex=None,
        content_type="application/json",
        http_version="HTTP/1.1",
        body_ciphertext=ciphertext,
        body_nonce=nonce,
        body_aad=aad,
    )


# ---------------------------------------------------------------------------
# Round-trip and metadata-only paths
# ---------------------------------------------------------------------------


class TestDecryptAndScrubRoundTrip:
    def test_decrypt_and_scrub_round_trip_redacts_email(self, tmp_path):
        """Encrypted JSON body containing an email address → export event
        with body_text containing a redaction placeholder (not the email)."""
        db_path, recording_id, kek, dek = _setup_recording(tmp_path)
        body = b'{"email":"alice@example.com","status":"ok"}'
        capture_event = _make_request_event(
            recording_id=recording_id, dek=dek, body=body,
        )

        with _kek_patch(kek):
            pipeline = NetworkScrubPipeline(db_path, recording_id)
            export_event = pipeline.decrypt_and_scrub(capture_event)

        # Returned class is the export-side counterpart, not the
        # capture-side class.
        assert isinstance(export_event, NetworkRequestExportEvent)
        # Public fields propagated through the model_dump round-trip.
        assert export_event.flow_id == "flow-aaa"
        assert export_event.method == "POST"
        assert export_event.host == "example.com"
        assert export_event.url == "https://example.com/api"
        # The plaintext body was scrubbed -- email is gone, replaced
        # with an entity tag.
        assert export_event.body_text is not None
        assert "alice@example.com" not in export_event.body_text
        assert "<EMAIL>" in export_event.body_text


class TestCaptureEventWithoutCiphertext:
    def test_v1_vintage_row_without_ciphertext_passes_through(
        self, tmp_path,
    ):
        """V1-vintage row (body_ciphertext=None) → export event with
        body_text=None (not raises)."""
        db_path, recording_id, kek, _dek = _setup_recording(tmp_path)
        capture_event = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-no-body",
            method="GET",
            url="https://example.com/",
            host="example.com",
            body_ciphertext=None,
            body_nonce=None,
            body_aad=None,
        )

        with _kek_patch(kek):
            pipeline = NetworkScrubPipeline(db_path, recording_id)
            export_event = pipeline.decrypt_and_scrub(capture_event)

        assert isinstance(export_event, NetworkRequestExportEvent)
        assert export_event.body_text is None


# ---------------------------------------------------------------------------
# Failure paths: AAD mismatch, ciphertext tampering, unsupported event
# ---------------------------------------------------------------------------


class TestAadMismatchYieldsNoneBodyText:
    def test_tampered_aad_yields_none_body_text(self, tmp_path, caplog):
        """Capture event with a body_aad that doesn't match the
        recomputed AAD → body_text is None and a debug log fires."""
        db_path, recording_id, kek, dek = _setup_recording(tmp_path)
        capture_event = _make_request_event(
            recording_id=recording_id, dek=dek, body=b"sensitive",
        )
        # Tamper with the AAD: rewrite to a value that the recomputed
        # AAD would not produce. The recomputed AAD is the canonical
        # form; replacing it triggers the AAD-mismatch branch.
        capture_event.body_aad = b"this-is-the-wrong-aad-bytes"

        with _kek_patch(kek), caplog.at_level(logging.DEBUG):
            pipeline = NetworkScrubPipeline(db_path, recording_id)
            export_event = pipeline.decrypt_and_scrub(capture_event)

        assert export_event.body_text is None
        # Debug log must mention AAD mismatch (not just generic skip).
        assert any(
            "AAD mismatch" in r.getMessage() for r in caplog.records
        )


class TestDecryptionFailureYieldsNoneBodyText:
    def test_tampered_ciphertext_yields_none_body_text(
        self, tmp_path, caplog,
    ):
        """Tampered ciphertext (AAD still valid) → InvalidTag → body_text
        is None and a debug log fires."""
        db_path, recording_id, kek, dek = _setup_recording(tmp_path)
        capture_event = _make_request_event(
            recording_id=recording_id, dek=dek, body=b"sensitive",
        )
        # Flip the last byte of the ciphertext so the GCM tag fails.
        ct = bytearray(capture_event.body_ciphertext)
        ct[-1] ^= 0xFF
        capture_event.body_ciphertext = bytes(ct)

        with _kek_patch(kek), caplog.at_level(logging.DEBUG):
            pipeline = NetworkScrubPipeline(db_path, recording_id)
            export_event = pipeline.decrypt_and_scrub(capture_event)

        assert export_event.body_text is None
        assert any(
            "decryption failed" in r.getMessage() for r in caplog.records
        )

    def test_missing_aad_yields_none_body_text(self, tmp_path, caplog):
        """Capture event with ciphertext but body_aad=None → integrity
        error path skips decrypt entirely."""
        db_path, recording_id, kek, dek = _setup_recording(tmp_path)
        capture_event = _make_request_event(
            recording_id=recording_id, dek=dek, body=b"sensitive",
        )
        capture_event.body_aad = None

        with _kek_patch(kek), caplog.at_level(logging.DEBUG):
            pipeline = NetworkScrubPipeline(db_path, recording_id)
            export_event = pipeline.decrypt_and_scrub(capture_event)

        assert export_event.body_text is None
        assert any(
            "body_aad is None" in r.getMessage() for r in caplog.records
        )


class TestUnsupportedEventType:
    def test_passing_non_network_event_raises_value_error(self, tmp_path):
        """A non-Network* event → ValueError."""
        from screencap.engine.events import MouseMoveEvent

        db_path, recording_id, kek, _dek = _setup_recording(tmp_path)
        with _kek_patch(kek):
            pipeline = NetworkScrubPipeline(db_path, recording_id)

        bogus = MouseMoveEvent(timestamp=1.0, x=10, y=20)
        with pytest.raises(ValueError, match="unsupported capture event"):
            pipeline.decrypt_and_scrub(bogus)


# ---------------------------------------------------------------------------
# KEK-unavailable paths (construction-time failures)
# ---------------------------------------------------------------------------


class TestKekUnavailableAtConstruction:
    def test_keyring_failure_raises_kek_unavailable(self, tmp_path):
        """When keyring.get_password raises (locked Keychain, cancelled
        dialog, no backend) → pipeline construction raises
        KekUnavailableError, NOT the underlying keyring exception."""
        db_path, recording_id, _kek, _dek = _setup_recording(tmp_path)

        with patch(
            "keyring.get_password",
            side_effect=RuntimeError("user cancelled"),
        ):
            with pytest.raises(KekUnavailableError, match="KEK unavailable"):
                NetworkScrubPipeline(db_path, recording_id)

    def test_dek_unwrap_failure_raises_kek_unavailable(self, tmp_path):
        """When the persisted wrapped DEK can't be unwrapped with the
        retrieved KEK (e.g., KEK rotated) → KekUnavailableError, not a
        bare InvalidTag from the crypto layer."""
        db_path, recording_id, _kek, _dek = _setup_recording(tmp_path)

        # Use a different KEK than what was used to wrap the DEK.
        wrong_kek = crypto._generate_kek()
        with _kek_patch(wrong_kek):
            with pytest.raises(KekUnavailableError, match="DEK unwrap failed"):
                NetworkScrubPipeline(db_path, recording_id)

    def test_missing_kek_raises_without_regenerating(self, tmp_path):
        """``get_kek`` returns None (entry removed by ``network remove-kek``)
        → KekUnavailableError surfaces with the actionable message AND
        ``keyring.set_password`` is never called.

        This is the load-bearing test for the read-only export-time KEK
        contract. With the prior ``get_or_create_kek`` semantics, a
        missing entry would silently regenerate a fresh KEK, then the
        unwrap would fail with InvalidTag (confusing) AND the user's
        ``network remove-kek`` would be effectively reversed by the
        next export attempt. The split into ``get_kek`` (read-only) +
        ``get_or_create_kek`` (capture-time only) closes that gap.
        """
        db_path, recording_id, _kek, _dek = _setup_recording(tmp_path)

        with (
            patch("keyring.get_password", return_value=None),
            patch("keyring.set_password") as mock_set,
        ):
            with pytest.raises(
                KekUnavailableError,
                match="not present in the Keychain",
            ):
                NetworkScrubPipeline(db_path, recording_id)
            mock_set.assert_not_called()


class TestMetaRowMissingRaises:
    def test_no_network_event_meta_row_raises_kek_unavailable(self, tmp_path):
        """Recording without a network_event_meta row (V1-vintage or
        non-network capture) → constructor raises KekUnavailableError
        with an actionable message."""
        # Build a recording.db with a Recording row but NO
        # NetworkEventMeta row.
        db_path = str(tmp_path / "recording.db")
        engine, Session = create_db(db_path)
        session = Session()
        try:
            rec = crud.insert_recording(session, {
                "timestamp": 1000.0,
                "platform": "darwin",
                "monitor_width": 1920,
                "monitor_height": 1080,
                "pixel_ratio": 2.0,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5.0,
            })
            recording_id = rec.id
        finally:
            session.close()
            engine.dispose()

        # No keyring patch needed; the meta-row check fires first.
        with pytest.raises(
            KekUnavailableError,
            match="no network_event_meta row",
        ):
            NetworkScrubPipeline(db_path, recording_id)


# ---------------------------------------------------------------------------
# recording_has_encrypted_bodies
# ---------------------------------------------------------------------------


class TestRecordingHasEncryptedBodies:
    """Regression for PR #157 review P2: meta-row presence is NOT a
    correct proxy for 'has encrypted bodies'. Pre-flight writes the
    meta row for every V1.5 --network recording, so a metadata-only
    capture (allowlist-miss) was incorrectly reported as encrypted —
    triggering spurious Keychain prompts at export and blocking
    `network remove-kek`.

    The fix queries actual ciphertext rows. The wrapped-DEK presence
    question is now ``recording_has_wrapped_dek``.
    """

    def test_returns_false_when_meta_row_absent(self, tmp_path):
        db_path = str(tmp_path / "recording.db")
        engine, Session = create_db(db_path)
        session = Session()
        try:
            rec = crud.insert_recording(session, {
                "timestamp": 1000.0,
                "platform": "darwin",
                "monitor_width": 1920,
                "monitor_height": 1080,
                "pixel_ratio": 2.0,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5.0,
            })
            recording_id = rec.id
        finally:
            session.close()
            engine.dispose()

        assert recording_has_encrypted_bodies(db_path, recording_id) is False

    def test_returns_false_when_meta_present_but_no_ciphertext_rows(
        self, tmp_path,
    ):
        """V1.5 metadata-only recording: the meta row exists (pre-flight
        wrote it) but no flow matched the body-capture allowlist, so
        every ``body_ciphertext`` is NULL. KEK access is unnecessary
        at export time.
        """
        db_path, recording_id, _kek, _dek = _setup_recording(tmp_path)
        # Insert a metadata-only row (no body_ciphertext).
        session = get_session_for_path(db_path)
        try:
            rec = session.query(Recording).first()
            crud.insert_network_event(session, rec, {
                "kind": "request",
                "flow_id": "f1",
                "method": "GET",
                "url": "https://random.example.com/",
                "host": "random.example.com",
                "timestamp": 1.0,
                "timestamp_ns": 1_000_000_000,
            })
            crud.flush_buffers(session)
            session.commit()
        finally:
            session.close()
        assert recording_has_encrypted_bodies(db_path, recording_id) is False
        # The wrapped DEK is on disk regardless — that's a separate question.
        assert recording_has_wrapped_dek(db_path, recording_id) is True

    def test_returns_false_on_pre_network_legacy_db(self, tmp_path):
        """Truly pre-V1 DB: ``network_event`` table doesn't exist at
        all. ``_migrate_schema`` only adds columns to existing tables,
        not missing tables, so the table stays missing on legacy
        recordings. The helper must catch the OperationalError and
        return False rather than crash. Same legacy path applies to
        ``recording_has_wrapped_dek`` (network_event_meta table).
        """
        import sqlite3
        db_path = str(tmp_path / "legacy.db")
        # Bare-minimum recording table; no network_event /
        # network_event_meta tables.
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO recording (id) VALUES (1)")
        conn.commit()
        conn.close()

        # Both helpers must return False without raising.
        assert recording_has_encrypted_bodies(db_path, 1) is False
        assert recording_has_wrapped_dek(db_path, 1) is False

    def test_returns_true_when_at_least_one_ciphertext_row(self, tmp_path):
        """Recording with at least one row carrying ciphertext requires
        KEK access at export time and blocks remove-kek."""
        db_path, recording_id, _kek, _dek = _setup_recording(tmp_path)
        session = get_session_for_path(db_path)
        try:
            rec = session.query(Recording).first()
            crud.insert_network_event(session, rec, {
                "kind": "request",
                "flow_id": "f1",
                "method": "POST",
                "url": "https://api.github.com/x",
                "host": "api.github.com",
                "body_ciphertext": b"\xde\xad\xbe\xef" * 4,
                "body_nonce": b"\x01" * 12,
                "body_aad": b"some-aad",
                "timestamp": 1.0,
                "timestamp_ns": 1_000_000_000,
            })
            crud.flush_buffers(session)
            session.commit()
        finally:
            session.close()
        assert recording_has_encrypted_bodies(db_path, recording_id) is True
        assert recording_has_wrapped_dek(db_path, recording_id) is True


# ---------------------------------------------------------------------------
# All four capture event types decrypt successfully
# ---------------------------------------------------------------------------


class TestPipelineProcessesAllFourCaptureEventTypes:
    """Parametrized over Request / Response / WSUpgrade / WSFrame."""

    @pytest.mark.parametrize(
        "kind,event_type_value,capture_factory,expected_export_cls",
        [
            (
                "request",
                "network.request",
                lambda recording_id, dek, body: _make_request_event(
                    recording_id=recording_id, dek=dek, body=body,
                ),
                NetworkRequestExportEvent,
            ),
            (
                "response",
                "network.response",
                lambda recording_id, dek, body: _make_response_event(
                    recording_id=recording_id, dek=dek, body=body,
                ),
                NetworkResponseExportEvent,
            ),
            (
                "ws_upgrade",
                "network.ws_upgrade",
                lambda recording_id, dek, body: _make_ws_upgrade_event(
                    recording_id=recording_id, dek=dek, body=body,
                ),
                NetworkWebSocketUpgradeExportEvent,
            ),
            (
                "ws_frame",
                "network.ws_frame",
                lambda recording_id, dek, body: _make_ws_frame_event(
                    recording_id=recording_id, dek=dek, body=body,
                ),
                NetworkWebSocketFrameExportEvent,
            ),
        ],
    )
    def test_all_four_kinds_round_trip(
        self,
        tmp_path,
        kind,
        event_type_value,
        capture_factory,
        expected_export_cls,
    ):
        db_path, recording_id, kek, dek = _setup_recording(tmp_path)
        body = b'{"key":"value"}'
        capture_event = capture_factory(recording_id, dek, body)

        with _kek_patch(kek):
            pipeline = NetworkScrubPipeline(db_path, recording_id)
            export_event = pipeline.decrypt_and_scrub(capture_event)

        assert isinstance(export_event, expected_export_cls)
        # Plaintext propagated (no PII to scrub in this fixture).
        assert export_event.body_text is not None


def _make_response_event(
    *, recording_id, dek, body, flow_id="flow-resp", timestamp_ns=2_000_000_000,
):
    aad = crypto.aad_bytes(
        recording_id=recording_id,
        flow_id=flow_id,
        event_type="network.response",
        ts_ns=timestamp_ns,
    )
    ciphertext, nonce = crypto.encrypt_body(dek, body, aad)
    return NetworkResponseEvent(
        timestamp=timestamp_ns / 1e9,
        timestamp_ns=timestamp_ns,
        flow_id=flow_id,
        host="example.com",
        status=200,
        headers=[],
        body_size=len(body),
        content_type="application/json",
        http_version="HTTP/1.1",
        body_ciphertext=ciphertext,
        body_nonce=nonce,
        body_aad=aad,
    )


def _make_ws_upgrade_event(
    *, recording_id, dek, body, flow_id="flow-ws-up", timestamp_ns=3_000_000_000,
):
    aad = crypto.aad_bytes(
        recording_id=recording_id,
        flow_id=flow_id,
        event_type="network.ws_upgrade",
        ts_ns=timestamp_ns,
    )
    ciphertext, nonce = crypto.encrypt_body(dek, body, aad)
    return NetworkWebSocketUpgradeEvent(
        timestamp=timestamp_ns / 1e9,
        timestamp_ns=timestamp_ns,
        flow_id=flow_id,
        url="wss://example.com/socket",
        host="example.com",
        status=101,
        headers=[],
        http_version="HTTP/1.1",
        details_json=None,
        body_ciphertext=ciphertext,
        body_nonce=nonce,
        body_aad=aad,
    )


def _make_ws_frame_event(
    *, recording_id, dek, body, flow_id="flow-ws-frame", timestamp_ns=4_000_000_000,
):
    aad = crypto.aad_bytes(
        recording_id=recording_id,
        flow_id=flow_id,
        event_type="network.ws_frame",
        ts_ns=timestamp_ns,
    )
    ciphertext, nonce = crypto.encrypt_body(dek, body, aad)
    return NetworkWebSocketFrameEvent(
        timestamp=timestamp_ns / 1e9,
        timestamp_ns=timestamp_ns,
        flow_id=flow_id,
        host="example.com",
        direction="received",
        frame_type="text",
        body_size=len(body),
        body_ciphertext=ciphertext,
        body_nonce=nonce,
        body_aad=aad,
    )
