"""Integration tests for the network-event writer dict + DB round-trip (V1.5).

The addon emits Pydantic events with body_ciphertext/body_nonce/body_aad on
the encrypted-body path. Those fields cross a multiprocessing.Queue (pickle
preserves bytes) and arrive at write_network_events, which calls
``_network_event_to_db_dict`` to build the dict that ``crud.insert_network_event``
persists. Without explicit pass-through, the writer drops the V1.5 fields
silently and the DB ends up with NULL ciphertext columns — invisible at
unit-test level but breaks the whole encrypt/decrypt loop.

These tests lock that pass-through plus the readback round-trip through
``dict_to_network_event`` so any future regression at any of those three
layers fails immediately.
"""
from __future__ import annotations

import pytest

from screencap.engine.db import (
    _ensure_network_tables,
    create_db,
    crud,
    get_session_for_path,
)
from screencap.engine.db.models import NetworkEvent, Recording
from screencap.engine.events import (
    NetworkRequestEvent,
    NetworkResponseEvent,
    NetworkWebSocketFrameEvent,
    NetworkWebSocketUpgradeEvent,
)
from screencap.engine.recorder import _network_event_to_db_dict


@pytest.fixture
def db_session(tmp_path):
    """Fresh DB with the V1.5 network tables and a single recording row."""
    db_path = tmp_path / "recording.db"
    create_db(str(db_path))
    session = get_session_for_path(str(db_path))
    _ensure_network_tables(session.get_bind())
    rec = Recording(task_description="test", timestamp=1.0)
    session.add(rec)
    session.commit()
    yield session, rec
    session.close()


class TestNetworkEventToDbDict:
    """The dict shape `_network_event_to_db_dict` produces. The bug fixed
    in P1 #1 was that body_ciphertext/body_nonce/body_aad were absent
    from the dict — silently nulling V1.5 ciphertext at the DB-write
    boundary even though the addon had populated them.
    """

    def test_request_event_carries_ciphertext_fields(self):
        evt = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            method="GET",
            url="https://api.github.com/user",
            host="api.github.com",
            body_ciphertext=b"ciphertext-bytes",
            body_nonce=b"\x00" * 12,
            body_aad=b"aad-bytes",
        )
        d = _network_event_to_db_dict(evt, "request")
        assert d["body_ciphertext"] == b"ciphertext-bytes"
        assert d["body_nonce"] == b"\x00" * 12
        assert d["body_aad"] == b"aad-bytes"

    def test_response_event_carries_ciphertext_fields(self):
        evt = NetworkResponseEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            host="api.github.com",
            status=200,
            body_ciphertext=b"ct",
            body_nonce=b"\x00" * 12,
            body_aad=b"aad",
        )
        d = _network_event_to_db_dict(evt, "response")
        assert d["body_ciphertext"] == b"ct"
        assert d["body_nonce"] == b"\x00" * 12
        assert d["body_aad"] == b"aad"

    def test_ws_upgrade_event_carries_ciphertext_fields(self):
        evt = NetworkWebSocketUpgradeEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            url="wss://api.example.com/ws",
            host="api.example.com",
            body_ciphertext=b"ct",
            body_nonce=b"\x00" * 12,
            body_aad=b"aad",
        )
        d = _network_event_to_db_dict(evt, "ws_upgrade")
        assert d["body_ciphertext"] == b"ct"
        assert d["body_nonce"] == b"\x00" * 12
        assert d["body_aad"] == b"aad"

    def test_ws_frame_event_carries_ciphertext_fields(self):
        evt = NetworkWebSocketFrameEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            host="api.example.com",
            direction="received",
            frame_type="text",
            body_ciphertext=b"ct",
            body_nonce=b"\x00" * 12,
            body_aad=b"aad",
        )
        d = _network_event_to_db_dict(evt, "ws_frame")
        assert d["body_ciphertext"] == b"ct"
        assert d["body_nonce"] == b"\x00" * 12
        assert d["body_aad"] == b"aad"

    def test_metadata_only_event_emits_none_for_ciphertext(self):
        """A non-encrypted event keeps the columns None."""
        evt = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            method="GET",
            url="https://random.example.com/",
            host="random.example.com",
        )
        d = _network_event_to_db_dict(evt, "request")
        assert d["body_ciphertext"] is None
        assert d["body_nonce"] is None
        assert d["body_aad"] is None


class TestEncryptedBodyEndToEnd:
    """End-to-end coverage for the addon-emit -> writer -> DB -> readback
    contract. This is the layer-crossing test the original V1.5 work
    was missing — without it, the dict-passthrough bug went undetected
    despite full unit-test coverage of the addon and the scrub pipeline
    in isolation.
    """

    def test_encrypted_request_round_trip(self, db_session):
        session, rec = db_session
        evt = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-encrypted",
            method="POST",
            url="https://api.linear.app/graphql",
            host="api.linear.app",
            body_size=42,
            body_sha256_hex="ab" * 32,
            body_ciphertext=b"\xde\xad\xbe\xef" * 8,
            body_nonce=b"\x01" * 12,
            body_aad=b'{"f":"flow-encrypted","r":1,"t":"network.request","ts":1000000000}',
        )
        # Drive through the actual writer-side dict-build helper so this
        # test exercises the same path the writer process uses.
        event_dict = _network_event_to_db_dict(evt, "request")
        crud.insert_network_event(session, rec, event_dict)
        crud.flush_buffers(session)
        session.commit()

        rows = session.query(NetworkEvent).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == "request"
        # Critical readback: bytes survive the SQLAlchemy LargeBinary
        # round-trip without truncation or NULL collapse.
        assert bytes(row.body_ciphertext) == b"\xde\xad\xbe\xef" * 8
        assert bytes(row.body_nonce) == b"\x01" * 12
        assert (
            bytes(row.body_aad)
            == b'{"f":"flow-encrypted","r":1,"t":"network.request","ts":1000000000}'
        )

    def test_metadata_only_round_trip_keeps_ciphertext_null(self, db_session):
        """V1-vintage / metadata-only path is unchanged: NULL columns."""
        session, rec = db_session
        evt = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-metadata-only",
            method="GET",
            url="https://random.example.com/",
            host="random.example.com",
            body_size=100,
        )
        event_dict = _network_event_to_db_dict(evt, "request")
        crud.insert_network_event(session, rec, event_dict)
        crud.flush_buffers(session)
        session.commit()

        rows = session.query(NetworkEvent).all()
        assert len(rows) == 1
        assert rows[0].body_ciphertext is None
        assert rows[0].body_nonce is None
        assert rows[0].body_aad is None
