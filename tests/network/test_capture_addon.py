"""Tests for screencap.network.capture_addon (V1 — metadata-only).

We mock mitmproxy's HTTPFlow / WebSocketMessage shapes minimally rather
than importing the real classes (heavy import + non-trivial construction).
The addon duck-types its arguments, so the fakes only need the attributes
the hooks actually read.
"""

from __future__ import annotations

import asyncio
import hashlib
import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from screencap.engine.events import (
    NetworkDropBurstEvent,
    NetworkPinFailureEvent,
    NetworkRequestEvent,
    NetworkResponseEvent,
    NetworkWebSocketFrameEvent,
    NetworkWebSocketUpgradeEvent,
)
from screencap.network.capture_addon import (
    NetworkCapture,
    _is_tls_pin_failure,
    make_hash_transformer,
)
from screencap.network.config import NetworkConfig
from screencap.privacy.policy import PrivacyConfig

# ---------------------------------------------------------------------------
# Test fakes — minimal duck types for HTTPFlow / Headers / WS messages
# ---------------------------------------------------------------------------


class FakeHeaders:
    """Mimics mitmproxy.http.Headers behavior the addon needs.

    Stores fields as a list of (name, value) string tuples; preserves
    multi-values on iteration. ``get()`` does a case-insensitive lookup
    of the first match. ``items(multi=True)`` returns all entries.
    """

    def __init__(self, fields: list[tuple[str, str]] | None = None) -> None:
        self._fields: list[tuple[str, str]] = list(fields or [])

    def get(self, name: str, default: Any = None) -> Any:
        lname = name.lower()
        for k, v in self._fields:
            if k.lower() == lname:
                return v
        return default

    def items(self, multi: bool = False) -> list[tuple[str, str]]:
        if multi:
            return list(self._fields)
        # Without multi=True we'd dedupe, but the addon always passes
        # multi=True so this branch is mostly defensive.
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for k, v in self._fields:
            kl = k.lower()
            if kl in seen:
                continue
            seen.add(kl)
            out.append((k, v))
        return out


@dataclass
class FakeRequest:
    method: str = "GET"
    url: str = "https://example.com/path"
    host: str = "example.com"
    headers: FakeHeaders = field(default_factory=FakeHeaders)
    raw_content: bytes = b""
    content: bytes | None = None
    http_version: str = "HTTP/1.1"
    stream: Any = None  # set by the addon when streaming


@dataclass
class FakeResponse:
    status_code: int = 200
    headers: FakeHeaders = field(default_factory=FakeHeaders)
    raw_content: bytes = b""
    content: bytes | None = None
    http_version: str = "HTTP/1.1"
    stream: Any = None


@dataclass
class FakeError:
    msg: str = ""


@dataclass
class FakeWSMessage:
    content: bytes = b""
    from_client: bool = True
    is_text: bool = True


@dataclass
class FakeWSData:
    messages: list[FakeWSMessage] = field(default_factory=list)


@dataclass
class FakeFlow:
    id: str = "flow-0001"
    request: FakeRequest = field(default_factory=FakeRequest)
    response: FakeResponse | None = None
    error: FakeError | None = None
    websocket: FakeWSData | None = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain(out_q: queue.Queue) -> list[Any]:
    drained: list[Any] = []
    while True:
        try:
            drained.append(out_q.get_nowait())
        except queue.Empty:
            return drained


def _make_capture(
    *,
    body_size_cap: int = 100_000,
    extra_blocklist: set[str] | None = None,
    mask_domains: set[str] | None = None,
    log_path: Path | None = None,
    qsize: int = 100,
    dek: bytes | None = None,
    capture_bodies_for: set[str] | None = None,
    override_default_capture_bodies_for: bool = True,
) -> tuple[NetworkCapture, queue.Queue]:
    # Synchronous queue (queue.Queue) for tests - mp.Queue uses an async
    # feeder thread which causes flaky get_nowait reads in single-test
    # runs. The addon duck-types put_nowait / put / get_nowait, so a
    # stdlib queue.Queue is functionally identical from its perspective.
    out_q: queue.Queue = queue.Queue(maxsize=qsize)
    nc = NetworkConfig(
        extra_blocklist=frozenset(extra_blocklist or set()),
        proxy_port=8080,
        override_default_blocklist=True,  # tests opt out of curated list
        body_size_cap=body_size_cap,
        capture_bodies_for=frozenset(capture_bodies_for or set()),
        # Default True so V1.5 tests opt out of the curated allowlist;
        # individual tests that need the curated list set this to False.
        override_default_capture_bodies_for=override_default_capture_bodies_for,
    )
    pc = PrivacyConfig(
        mask_domains=frozenset(mask_domains or set()),
    )
    return (
        NetworkCapture(
            out_q=out_q,
            recording_id=42,
            network_config=nc,
            privacy_config=pc,
            log_path=log_path,
            dek=dek,
        ),
        out_q,
    )


# ---------------------------------------------------------------------------
# make_hash_transformer
# ---------------------------------------------------------------------------


class TestMakeHashTransformer:
    def test_aggregates_chunks(self):
        state: dict = {}
        h = make_hash_transformer("flow-1", "req", state)
        # Simulate mitmproxy invoking the transformer per chunk.
        out1 = h(b"hello ")
        out2 = h(b"world")
        h(b"")  # terminal
        assert out1 == b"hello "
        assert out2 == b"world"
        expected = hashlib.sha256(b"hello world").digest()
        assert state["flow-1"]["req_sha"] == expected
        assert state["flow-1"]["req_size"] == 11

    def test_terminal_empty_signals_finalize(self):
        state: dict = {}
        h = make_hash_transformer("f", "resp", state)
        h(b"abc")
        # No state until the terminal call.
        assert "resp_sha" not in state.get("f", {})
        h(b"")
        assert state["f"]["resp_sha"] == hashlib.sha256(b"abc").digest()
        assert state["f"]["resp_size"] == 3

    def test_returns_chunks_unchanged(self):
        state: dict = {}
        h = make_hash_transformer("f", "req", state)
        for chunk in (b"a" * 100, b"b" * 50, b"c" * 25):
            assert h(chunk) == chunk


# ---------------------------------------------------------------------------
# Blocked host (forward-but-don't-record)
# ---------------------------------------------------------------------------


class TestBlockedHost:
    def test_extra_blocklist_blocks_request(self, tmp_path):
        log_path = tmp_path / "log.txt"
        capture, out_q = _make_capture(
            extra_blocklist={"example.com"}, log_path=log_path
        )
        flow = FakeFlow(request=FakeRequest(host="example.com"))
        capture.requestheaders(flow)
        capture.request(flow)
        capture.response(flow)
        # No event emitted.
        assert _drain(out_q) == []
        # flow marked blocked.
        assert flow.metadata.get("screencap_blocked") is True
        # Log line appended.
        assert "blocked HTTP host" in log_path.read_text()

    def test_mask_domains_blocks_request(self):
        capture, out_q = _make_capture(mask_domains={"example.com"})
        flow = FakeFlow(request=FakeRequest(host="api.example.com"))
        capture.requestheaders(flow)
        capture.request(flow)
        assert _drain(out_q) == []
        assert flow.metadata["screencap_blocked"] is True


# ---------------------------------------------------------------------------
# Happy-path HTTP flow
# ---------------------------------------------------------------------------


class TestHTTPFlow:
    def test_get_with_small_response_body(self):
        capture, out_q = _make_capture()
        body = b'{"hello":"world"}'
        flow = FakeFlow(
            request=FakeRequest(
                method="GET",
                url="https://example.com/api",
                host="example.com",
                headers=FakeHeaders(
                    [
                        ("Host", "example.com"),
                        ("Accept", "application/json"),
                    ]
                ),
                raw_content=b"",
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders(
                    [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ]
                ),
                raw_content=body,
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        assert len(events) == 2
        req, resp = events
        assert isinstance(req, NetworkRequestEvent)
        assert req.method == "GET"
        assert req.host == "example.com"
        assert req.body_size == 0
        assert req.body_sha256_hex == hashlib.sha256(b"").hexdigest()
        assert isinstance(resp, NetworkResponseEvent)
        assert resp.status == 200
        assert resp.body_size == len(body)
        assert resp.body_sha256_hex == hashlib.sha256(body).hexdigest()
        assert resp.content_type == "application/json"

    def test_authorization_header_redacted(self):
        capture, out_q = _make_capture()
        flow = FakeFlow(
            request=FakeRequest(
                headers=FakeHeaders(
                    [
                        ("Authorization", "Bearer abc123"),
                        ("Accept", "*/*"),
                    ]
                ),
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        req = next(e for e in events if isinstance(e, NetworkRequestEvent))
        names = {n.lower(): v for n, v in req.headers}
        assert names["authorization"] == "[REDACTED:auth-header]"
        assert names["accept"] == "*/*"

    def test_url_query_param_redacted(self):
        capture, out_q = _make_capture()
        flow = FakeFlow(
            request=FakeRequest(
                url="https://example.com/api?token=abc&keep=ok",
                headers=FakeHeaders(),
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        req = next(e for e in events if isinstance(e, NetworkRequestEvent))
        assert "token=%5BREDACTED:query-param%5D" in req.url or \
               "token=[REDACTED:query-param]" in req.url
        assert "keep=ok" in req.url

    def test_multi_value_set_cookie_preserved(self):
        capture, out_q = _make_capture()
        flow = FakeFlow(
            request=FakeRequest(headers=FakeHeaders()),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders(
                    [
                        ("Set-Cookie", "a=1"),
                        ("Set-Cookie", "b=2"),
                        ("Content-Length", "0"),
                    ]
                ),
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        resp = next(e for e in events if isinstance(e, NetworkResponseEvent))
        cookies = [v for n, v in resp.headers if n.lower() == "set-cookie"]
        # Both Set-Cookie entries preserved (each redacted to the same
        # placeholder; the LIST shape and count are what matter).
        assert len(cookies) == 2
        assert all(v == "[REDACTED:auth-header]" for v in cookies)

    def test_case_insensitive_auth_header_redaction(self):
        capture, out_q = _make_capture()
        for name in ("AUTHORIZATION", "authorization", "Authorization"):
            flow = FakeFlow(
                id=f"flow-{name}",
                request=FakeRequest(
                    headers=FakeHeaders([(name, "Bearer abc")]),
                ),
                response=FakeResponse(
                    status_code=200,
                    headers=FakeHeaders([("Content-Length", "0")]),
                ),
            )
            capture.requestheaders(flow)
            capture.request(flow)
            capture.responseheaders(flow)
            capture.response(flow)

        events = _drain(out_q)
        reqs = [e for e in events if isinstance(e, NetworkRequestEvent)]
        assert len(reqs) == 3
        for r in reqs:
            v = next(v for n, v in r.headers if n.lower() == "authorization")
            assert v == "[REDACTED:auth-header]"

    def test_empty_body(self):
        capture, out_q = _make_capture()
        flow = FakeFlow(
            request=FakeRequest(
                headers=FakeHeaders([("Content-Length", "0")]),
                raw_content=b"",
            ),
            response=FakeResponse(
                status_code=204,
                headers=FakeHeaders([("Content-Length", "0")]),
                raw_content=b"",
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        for e in events:
            assert e.body_size == 0
            assert e.body_sha256_hex == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Hash basis = wire bytes (gzip case)
# ---------------------------------------------------------------------------


class TestHashBasisWireBytes:
    def test_compressed_response_hash_uses_raw_content(self):
        # Wire payload is 20 KB compressed; "decoded" size irrelevant.
        wire = b"\x1f\x8b" + b"\x00" * (20_000 - 2)
        capture, out_q = _make_capture()
        flow = FakeFlow(
            request=FakeRequest(headers=FakeHeaders()),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders(
                    [
                        ("Content-Type", "application/json"),
                        ("Content-Encoding", "gzip"),
                        ("Content-Length", str(len(wire))),
                    ]
                ),
                raw_content=wire,
                content=b"x" * 200_000,  # decoded (irrelevant)
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        resp = next(e for e in events if isinstance(e, NetworkResponseEvent))
        assert resp.body_size == len(wire)
        assert resp.body_sha256_hex == hashlib.sha256(wire).hexdigest()


# ---------------------------------------------------------------------------
# Streaming (chunk-hashing transformer install)
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_large_content_length_installs_transformer(self):
        capture, out_q = _make_capture(body_size_cap=100_000)
        flow = FakeFlow(
            request=FakeRequest(headers=FakeHeaders()),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "100001")]),
                raw_content=b"",
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)

        # The transformer was installed.
        assert callable(flow.response.stream)

        # Send wire bytes through it in chunks.
        chunk1 = b"abc" * 1000
        chunk2 = b"xyz" * 500
        flow.response.stream(chunk1)
        flow.response.stream(chunk2)
        flow.response.stream(b"")  # terminal

        # State should now reflect aggregated hash.
        expected_sha = hashlib.sha256(chunk1 + chunk2).digest()
        expected_size = len(chunk1) + len(chunk2)
        # NB: capture._stream_state is keyed by flow.id and survives
        # until response() runs and pops it. We invoke response() now.
        capture.response(flow)

        events = _drain(out_q)
        resp = next(e for e in events if isinstance(e, NetworkResponseEvent))
        assert resp.body_size == expected_size
        assert resp.body_sha256_hex == expected_sha.hex()

    def test_chunked_encoding_always_streams(self):
        capture, _ = _make_capture()
        flow = FakeFlow(
            request=FakeRequest(headers=FakeHeaders()),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Transfer-Encoding", "chunked")]),
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        # Transformer installed because Content-Length missing AND
        # Transfer-Encoding: chunked.
        assert callable(flow.response.stream)

    def test_small_content_length_does_not_install(self):
        capture, _ = _make_capture(body_size_cap=100_000)
        body = b"x" * 50
        flow = FakeFlow(
            request=FakeRequest(headers=FakeHeaders()),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", str(len(body)))]),
                raw_content=body,
            ),
        )
        capture.responseheaders(flow)
        # No transformer for small bodies.
        assert flow.response.stream is None


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


class TestWebSocket:
    def _make_ws_flow(
        self,
        *,
        upgrade_request: bool = True,
        msgs: list[FakeWSMessage] | None = None,
    ) -> FakeFlow:
        req_headers = FakeHeaders(
            [
                ("Host", "example.com"),
                ("Upgrade", "websocket") if upgrade_request else ("Accept", "*/*"),
                ("Connection", "Upgrade"),
                ("Sec-WebSocket-Key", "x3JJHMbDL1EzLkh9GBhXDw=="),
            ]
        )
        return FakeFlow(
            id="ws-flow-1",
            request=FakeRequest(
                method="GET",
                url="ws://example.com/ws",
                host="example.com",
                headers=req_headers,
                raw_content=b"",
            ),
            response=FakeResponse(
                status_code=101,
                headers=FakeHeaders(
                    [
                        ("Upgrade", "websocket"),
                        ("Connection", "Upgrade"),
                        ("Sec-WebSocket-Accept", "abc123"),
                    ]
                ),
            ),
            websocket=FakeWSData(messages=list(msgs or [])),
        )

    def test_upgrade_event_emitted_no_duplicate_response(self):
        capture, out_q = _make_capture()
        flow = self._make_ws_flow()
        # Lifecycle: requestheaders, request, responseheaders, response,
        # websocket_start.
        capture.requestheaders(flow)
        # `request()` emits a NetworkRequestEvent for the GET upgrade.
        capture.request(flow)
        capture.responseheaders(flow)
        # `response()` should be SUPPRESSED for the 101.
        capture.response(flow)
        capture.websocket_start(flow)

        events = _drain(out_q)
        kinds = [type(e).__name__ for e in events]
        # Exactly one upgrade event; no NetworkResponseEvent for the 101.
        assert kinds.count("NetworkWebSocketUpgradeEvent") == 1
        assert "NetworkResponseEvent" not in kinds

        upgrade = next(
            e for e in events if isinstance(e, NetworkWebSocketUpgradeEvent)
        )
        assert upgrade.status == 101
        assert upgrade.url == "ws://example.com/ws"
        # Response headers stored in headers list.
        names_resp = [n.lower() for n, _ in upgrade.headers]
        assert "upgrade" in names_resp
        # Request headers stored in details_json.
        rh = upgrade.details_json["request_headers"]
        assert ["Upgrade", "websocket"] in rh

    def test_text_frame_sent(self):
        capture, out_q = _make_capture()
        flow = self._make_ws_flow(
            msgs=[FakeWSMessage(content=b"hello", from_client=True, is_text=True)]
        )
        capture.websocket_message(flow)
        events = _drain(out_q)
        assert len(events) == 1
        e = events[0]
        assert isinstance(e, NetworkWebSocketFrameEvent)
        assert e.direction == "sent"
        assert e.frame_type == "text"
        assert e.body_size == 5
        assert e.body_sha256_hex == hashlib.sha256(b"hello").hexdigest()
        # Message popped — no leak.
        assert flow.websocket.messages == []

    def test_binary_frame_received(self):
        capture, out_q = _make_capture()
        flow = self._make_ws_flow(
            msgs=[FakeWSMessage(content=b"\x00\x01\x02", from_client=False, is_text=False)]
        )
        capture.websocket_message(flow)
        events = _drain(out_q)
        e = events[0]
        assert e.direction == "received"
        assert e.frame_type == "binary"
        assert flow.websocket.messages == []

    def test_message_popped_unconditionally_on_drop(self):
        # Tiny queue → second WS push causes a drop, but the message
        # MUST still be popped from flow.websocket.messages.
        capture, out_q = _make_capture(qsize=1)
        flow = self._make_ws_flow(
            msgs=[FakeWSMessage(content=b"first", from_client=True)]
        )
        capture.websocket_message(flow)  # fills the queue
        # Second message — out_q is full.
        flow.websocket.messages.append(
            FakeWSMessage(content=b"second", from_client=True)
        )
        capture.websocket_message(flow)
        # Second message popped (memory bound).
        assert flow.websocket.messages == []
        assert capture._dropped_count == 1


# ---------------------------------------------------------------------------
# Drop-burst path
# ---------------------------------------------------------------------------


class TestDropBurst:
    def test_full_queue_increments_drop_count(self):
        capture, out_q = _make_capture(qsize=1)
        # First request fills the queue.
        flow1 = FakeFlow(
            id="f-1",
            request=FakeRequest(host="example.com", headers=FakeHeaders()),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
            ),
        )
        capture.requestheaders(flow1)
        capture.request(flow1)
        # Second request — out_q is full now (request event from #1).
        flow2 = FakeFlow(
            id="f-2",
            request=FakeRequest(host="other.example.com", headers=FakeHeaders()),
        )
        capture.requestheaders(flow2)
        capture.request(flow2)
        # The second request was dropped.
        assert capture._dropped_count == 1
        assert "other.example.com" in capture._dropped_hosts

    def test_tick_emits_drop_burst_event(self):
        capture, out_q = _make_capture(qsize=10)
        # Drain queue while we manually book drops.
        capture._record_drop("a.example.com")
        capture._record_drop("b.example.com")
        capture._record_drop("a.example.com")
        capture._tick_drop_burst()

        events = _drain(out_q)
        bursts = [e for e in events if isinstance(e, NetworkDropBurstEvent)]
        assert len(bursts) == 1
        b = bursts[0]
        assert b.details_json["source"] == "addon"
        assert b.details_json["dropped_count"] == 3
        assert set(b.details_json["hosts_affected"]) == {
            "a.example.com",
            "b.example.com",
        }
        # Counters reset.
        assert capture._dropped_count == 0
        assert capture._dropped_hosts == set()

    def test_tick_with_no_drops_emits_nothing(self):
        capture, out_q = _make_capture()
        capture._tick_drop_burst()
        # No event because nothing was dropped.
        assert _drain(out_q) == []


# ---------------------------------------------------------------------------
# error() — TLS pinning detection
# ---------------------------------------------------------------------------


class TestError:
    def test_tls_pin_failure_indicators(self):
        assert _is_tls_pin_failure("tlsv1 alert certificate unknown") is True
        assert _is_tls_pin_failure("TLSv1 ALERT Certificate Unknown") is True
        assert _is_tls_pin_failure("ssl handshake failure") is True
        assert _is_tls_pin_failure("connection refused") is False
        assert _is_tls_pin_failure("") is False

    def test_pin_failure_emits_event_once_per_host(self, tmp_path):
        log_path = tmp_path / "log.txt"
        capture, out_q = _make_capture(log_path=log_path)
        flow1 = FakeFlow(
            id="f-1",
            request=FakeRequest(host="pinned.example.com"),
            error=FakeError(msg="tlsv1 alert certificate unknown"),
        )
        capture.error(flow1)
        flow2 = FakeFlow(
            id="f-2",
            request=FakeRequest(host="pinned.example.com"),
            error=FakeError(msg="tlsv1 alert certificate unknown"),
        )
        capture.error(flow2)

        events = _drain(out_q)
        pin_events = [e for e in events if isinstance(e, NetworkPinFailureEvent)]
        assert len(pin_events) == 1
        assert pin_events[0].host == "pinned.example.com"
        # Host added to runtime tunnel hosts.
        assert "pinned.example.com" in capture._runtime_tunnel_hosts
        # Both errors logged.
        log = log_path.read_text()
        assert log.count("flow error host=pinned.example.com") == 2

    def test_error_pops_stream_state(self):
        capture, _ = _make_capture()
        flow = FakeFlow(
            id="leak-flow",
            request=FakeRequest(host="example.com"),
            error=FakeError(msg="connection reset"),
        )
        # Simulate transformer was installed and partial state exists.
        capture._stream_state["leak-flow"] = {"req_sha": b"abc"}
        capture.error(flow)
        # State popped — no leak.
        assert "leak-flow" not in capture._stream_state


# ---------------------------------------------------------------------------
# tls_clienthello — runtime tunnel opt-out
# ---------------------------------------------------------------------------


class TestTlsClienthello:
    def test_sni_in_tunnel_set_sets_ignore_connection(self):
        capture, _ = _make_capture()
        capture._runtime_tunnel_hosts.add("pinned.example.com")

        @dataclass
        class FakeClientHello:
            sni: str = "pinned.example.com"

        @dataclass
        class FakeData:
            client_hello: FakeClientHello = field(default_factory=FakeClientHello)
            ignore_connection: bool = False

        d = FakeData()
        capture.tls_clienthello(d)
        assert d.ignore_connection is True

    def test_sni_not_in_tunnel_set_unchanged(self):
        capture, _ = _make_capture()

        @dataclass
        class FakeClientHello:
            sni: str = "example.com"

        @dataclass
        class FakeData:
            client_hello: FakeClientHello = field(default_factory=FakeClientHello)
            ignore_connection: bool = False

        d = FakeData()
        capture.tls_clienthello(d)
        assert d.ignore_connection is False

    def test_mixed_case_sni_matches_lowercased_cache(self):
        """V1.5 P2 fix: SNI is case-insensitive per RFC 6066. The
        persistent pinned-host cache stores lowercased entries (see
        pinned_hosts.load_known_pinned_hosts). The membership check
        in tls_clienthello must lowercase the SNI before comparing,
        or the cache that's supposed to prevent the per-recording
        first-failure is defeated for any client that sends mixed-case
        SNI (Pinned.Example.com would miss pinned.example.com).
        """
        capture, _ = _make_capture()
        capture._runtime_tunnel_hosts.add("pinned.example.com")

        @dataclass
        class FakeClientHello:
            sni: str = "Pinned.Example.COM"  # mixed case

        @dataclass
        class FakeData:
            client_hello: FakeClientHello = field(default_factory=FakeClientHello)
            ignore_connection: bool = False

        d = FakeData()
        capture.tls_clienthello(d)
        assert d.ignore_connection is True, (
            "mixed-case SNI must still match the lowercased cache; "
            "otherwise the persistent pinned-host cache breaks"
        )
        # The observed-set entry is also lowercased so done() emits
        # a single network.tunneled regardless of SNI casing.
        assert "pinned.example.com" in capture._observed_tunnel_hosts
        assert "Pinned.Example.COM" not in capture._observed_tunnel_hosts

    def test_observed_tunnel_hosts_records_only_seen(self):
        """V1.5 P2 #4: ``_observed_tunnel_hosts`` must NOT be seeded
        from the persistent cache. Only hosts whose tunnel was actually
        triggered during this recording (via tls_clienthello or error)
        end up in the observed set, so done() does not emit
        ``network.tunneled`` events for hosts the user never touched.
        """
        capture, _ = _make_capture()
        # Pre-populate the runtime cache (mirroring what __init__ does
        # from the persistent JSON file). The observed set must remain
        # empty until something actually fires.
        capture._runtime_tunnel_hosts.update({"cached-a.com", "cached-b.com"})
        assert capture._observed_tunnel_hosts == set()

        # tls_clienthello for a cached host marks it observed.
        @dataclass
        class FakeClientHello:
            sni: str = "cached-a.com"

        @dataclass
        class FakeData:
            client_hello: FakeClientHello = field(default_factory=FakeClientHello)
            ignore_connection: bool = False

        capture.tls_clienthello(FakeData())
        # Only the host that was actually seen ends up in the observed
        # set; the other cached host stays out.
        assert capture._observed_tunnel_hosts == {"cached-a.com"}
        # Started_at is populated for the observed host only.
        assert "cached-a.com" in capture._tunnel_started_at
        assert "cached-b.com" not in capture._tunnel_started_at


# ---------------------------------------------------------------------------
# done() — final flush + log
# ---------------------------------------------------------------------------


class TestDone:
    def test_done_flushes_pending_drops(self, tmp_path):
        log_path = tmp_path / "log.txt"
        capture, out_q = _make_capture(log_path=log_path)
        capture._record_drop("example.com")
        capture.done()

        events = _drain(out_q)
        bursts = [e for e in events if isinstance(e, NetworkDropBurstEvent)]
        assert len(bursts) == 1
        # Shutdown line written.
        assert "shutdown:" in log_path.read_text()

    def test_done_emits_tunneled_only_for_observed_hosts(self, tmp_path):
        """V1.5 P2 #4: ``done()`` iterates the observed set, not the
        full cache. Pre-loading 5 hosts in the cache and observing only
        1 must produce exactly 1 ``network.tunneled`` event.
        """
        from screencap.engine.events import NetworkTunneledEvent

        log_path = tmp_path / "log.txt"
        capture, out_q = _make_capture(log_path=log_path)
        # Cache has many old hosts; only one is observed this recording.
        capture._runtime_tunnel_hosts.update({
            "old-1.com", "old-2.com", "old-3.com",
            "old-4.com", "observed.com",
        })
        capture._observed_tunnel_hosts.add("observed.com")
        capture._tunnel_started_at["observed.com"] = 100.0

        capture.done()

        events = _drain(out_q)
        tunneled = [e for e in events if isinstance(e, NetworkTunneledEvent)]
        # Exactly one event, for the observed host only.
        assert len(tunneled) == 1
        assert tunneled[0].host == "observed.com"
        assert tunneled[0].started_at == 100.0

    def test_done_emits_zero_tunneled_when_nothing_observed(self, tmp_path):
        """Cache loaded but no host observed → zero network.tunneled."""
        from screencap.engine.events import NetworkTunneledEvent

        log_path = tmp_path / "log.txt"
        capture, out_q = _make_capture(log_path=log_path)
        capture._runtime_tunnel_hosts.update({
            "cached-a.com", "cached-b.com", "cached-c.com",
        })
        # _observed_tunnel_hosts intentionally empty.
        capture.done()

        events = _drain(out_q)
        tunneled = [e for e in events if isinstance(e, NetworkTunneledEvent)]
        assert tunneled == []

    def test_done_flushes_drops_caused_by_tunneled_emission(self, tmp_path):
        """V1.5 P3: tunneled emission can itself hit queue.Full and
        accumulate drops in _dropped_count. The first flush at the top
        of done() runs BEFORE tunneled emission, so without a second
        flush after, those shutdown-emission drops are silently lost.

        Approach: stub _enqueue so every tunneled put becomes a drop
        (simulates "queue is full for the duration of done()"), but
        leave _emit_drop_burst untouched so the second flush can reach
        out_q. Verify the second flush emits a burst with dropped_count
        equal to the observed-host count.
        """
        from screencap.engine.events import NetworkDropBurstEvent

        log_path = tmp_path / "log.txt"
        capture, out_q = _make_capture(log_path=log_path, qsize=20)

        # Three observed pinned hosts — the loop in done() will emit
        # three NetworkTunneledEvents and our stubbed _enqueue will
        # convert each into a drop.
        capture._observed_tunnel_hosts.update({
            "host-a.com", "host-b.com", "host-c.com",
        })
        capture._tunnel_started_at = {
            "host-a.com": 100.0,
            "host-b.com": 100.0,
            "host-c.com": 100.0,
        }

        # Stub _enqueue to count drops directly; bypass the real put.
        # _emit_drop_burst still uses self._out_q.put for the burst.
        original_record_drop = capture._record_drop
        def _stub_enqueue(event, *, host):  # noqa: ARG001
            original_record_drop(host)
        capture._enqueue = _stub_enqueue

        capture.done()

        events = _drain(out_q)
        bursts = [e for e in events if isinstance(e, NetworkDropBurstEvent)]
        assert len(bursts) == 1, (
            f"expected exactly one drop_burst from the second flush, "
            f"got {len(bursts)}"
        )
        burst = bursts[0]
        assert burst.details_json["dropped_count"] == 3, (
            f"expected 3 drops (one per observed host), got "
            f"{burst.details_json['dropped_count']}"
        )
        assert set(burst.details_json["hosts_affected"]) == {
            "host-a.com", "host-b.com", "host-c.com",
        }


# ---------------------------------------------------------------------------
# running() loop capture
# ---------------------------------------------------------------------------


class TestRunning:
    def test_running_no_loop(self):
        # Outside an asyncio loop, running() doesn't crash and leaves
        # _loop=None.
        capture, _ = _make_capture()
        capture.running()
        assert capture._loop is None
        assert capture._drop_burst_timer_handle is None

    def test_running_with_loop_schedules_timer(self):
        capture, _ = _make_capture()

        async def _drive() -> None:
            capture.running()
            assert capture._loop is asyncio.get_running_loop()
            assert capture._drop_burst_timer_handle is not None
            # Cancel before exiting so we don't leak the timer.
            capture._drop_burst_timer_handle.cancel()

        asyncio.run(_drive())


# ---------------------------------------------------------------------------
# V1.5 body capture (encryption)
# ---------------------------------------------------------------------------


class TestV15BodyCapture:
    """Body-encryption hot path. The Unit 4 brief enumerates the scenarios
    in /docs/tickets/medium-2026-04-27-feat-network-logging-v1.5-bodies.md.
    """

    _TEST_DEK = b"\x11" * 32  # deterministic 32-byte DEK for tests

    def test_dek_none_keeps_v1_metadata_only(self):
        # V1 caller (no DEK supplied). Even for an allowlisted host, the
        # event must NOT carry body_ciphertext / body_nonce / body_aad.
        capture, out_q = _make_capture(
            dek=None,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
        )
        body = b'{"hello":"world"}'
        flow = FakeFlow(
            request=FakeRequest(
                method="POST",
                host="example.com",
                headers=FakeHeaders([("Content-Length", str(len(body)))]),
                raw_content=body,
                content=body,
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
                raw_content=b"",
                content=b"",
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        for e in events:
            assert e.body_ciphertext is None
            assert e.body_nonce is None
            assert e.body_aad is None

    def test_allowlisted_small_body_round_trips(self):
        from screencap.network import crypto as _crypto

        body = b'{"hello":"world"}'
        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
        )
        flow = FakeFlow(
            id="round-trip-flow",
            request=FakeRequest(
                method="POST",
                url="https://example.com/api",
                host="example.com",
                headers=FakeHeaders([("Content-Length", str(len(body)))]),
                raw_content=body,
                content=body,
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
                raw_content=b"",
                content=b"",
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        req = next(e for e in events if isinstance(e, NetworkRequestEvent))

        assert req.body_ciphertext is not None
        assert req.body_nonce is not None and len(req.body_nonce) == 12
        assert req.body_aad is not None
        # body_size / body_sha256_hex on the WIRE bytes — unchanged from V1.
        assert req.body_size == len(body)
        assert req.body_sha256_hex == hashlib.sha256(body).hexdigest()

        plaintext = _crypto.decrypt_body(
            req.body_ciphertext, req.body_nonce, self._TEST_DEK, req.body_aad
        )
        assert plaintext == body

    def test_host_not_in_allowlist_metadata_only(self):
        body = b'{"some":"data"}'
        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
        )
        flow = FakeFlow(
            request=FakeRequest(
                method="POST",
                host="random.example.org",  # NOT in the allowlist
                headers=FakeHeaders([("Content-Length", str(len(body)))]),
                raw_content=body,
                content=body,
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
                raw_content=b"",
                content=b"",
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        req = next(e for e in events if isinstance(e, NetworkRequestEvent))
        assert req.body_ciphertext is None
        assert req.body_nonce is None
        assert req.body_aad is None
        # Metadata is still emitted as in V1.
        assert req.body_size == len(body)
        assert req.body_sha256_hex == hashlib.sha256(body).hexdigest()

    def test_blocklist_wins_over_allowlist(self):
        # User added "chase.com" to BOTH capture_bodies_for AND mask_domains
        # (or it's in DEFAULT_BLOCKLIST). Blocklist wins; the request is
        # blocked entirely (no event at all), but if we also exclude the
        # blocklist test scope by using mask_domains directly, requestheaders
        # marks the flow blocked. Test the precedence at the
        # is_host_in_capture_bodies_for layer: the body-capture predicate
        # MUST return False even when both lists match.
        body = b'{"sensitive":"data"}'
        # mask_domains here covers "chase.com" — that's the user's privacy
        # blocklist input. Same host is allowlisted but block wins.
        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"chase.com"},
            override_default_capture_bodies_for=True,
            mask_domains={"chase.com"},
        )
        flow = FakeFlow(
            request=FakeRequest(
                method="POST",
                host="chase.com",
                headers=FakeHeaders([("Content-Length", str(len(body)))]),
                raw_content=body,
                content=body,
            ),
        )
        capture.requestheaders(flow)
        # Blocked at requestheaders → no event emitted at all.
        capture.request(flow)

        events = _drain(out_q)
        # Either zero events (blocked entirely) or a metadata-only event
        # without ciphertext. The contract is: ciphertext MUST NOT appear.
        assert all(e.body_ciphertext is None for e in events)
        assert all(e.body_nonce is None for e in events)
        assert all(e.body_aad is None for e in events)

    def test_decoded_body_over_cap_metadata_only(self):
        # Compressed-bypass guard. Wire bytes pass the cap but decoded
        # bytes blow past it: body MUST NOT be encrypted.
        wire = b"\x1f\x8b" + b"\x00" * 5_000  # tiny gzip envelope
        decoded = b"x" * 200_001  # 200 KB decoded body
        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
            body_size_cap=100_000,
        )
        flow = FakeFlow(
            request=FakeRequest(
                method="GET",
                host="example.com",
                headers=FakeHeaders(),
                raw_content=b"",
                content=b"",
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders(
                    [
                        ("Content-Encoding", "gzip"),
                        ("Content-Length", str(len(wire))),
                    ]
                ),
                raw_content=wire,
                content=decoded,
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        resp = next(e for e in events if isinstance(e, NetworkResponseEvent))
        # Wire body_size still populated — V1 invariant unchanged.
        assert resp.body_size == len(wire)
        assert resp.body_sha256_hex == hashlib.sha256(wire).hexdigest()
        # Body NOT encrypted because decoded > cap.
        assert resp.body_ciphertext is None
        assert resp.body_nonce is None
        assert resp.body_aad is None

    def test_streaming_engaged_metadata_only(self):
        # Streaming was used → wire bytes were chunk-hashed; we don't
        # buffer to encrypt.
        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
            body_size_cap=100_000,
        )
        flow = FakeFlow(
            id="streamed-flow",
            request=FakeRequest(host="example.com", headers=FakeHeaders()),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "100001")]),
                raw_content=b"",
                # decoded set, but the streaming-engaged check should
                # short-circuit before we even look at decoded.
                content=b"x" * 100,
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)

        # The transformer was installed by responseheaders().
        assert callable(flow.response.stream)

        # Drive the transformer end-to-end so stream_state has resp_sha.
        chunk = b"abc" * 1000
        flow.response.stream(chunk)
        flow.response.stream(b"")  # terminal — finalizes hash
        assert "resp_sha" in capture._stream_state[flow.id]

        capture.response(flow)
        events = _drain(out_q)
        resp = next(e for e in events if isinstance(e, NetworkResponseEvent))
        # Streaming sha applied; encryption NOT engaged.
        assert resp.body_ciphertext is None
        assert resp.body_nonce is None
        assert resp.body_aad is None
        assert resp.body_size == len(chunk)

    def test_websocket_frame_encrypted_for_allowlisted_host(self):
        from screencap.network import crypto as _crypto

        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
        )
        payload = b"hello-ws"
        flow = FakeFlow(
            id="ws-flow-1",
            request=FakeRequest(
                method="GET",
                url="ws://example.com/ws",
                host="example.com",
                headers=FakeHeaders([("Upgrade", "websocket")]),
            ),
            websocket=FakeWSData(
                messages=[
                    FakeWSMessage(
                        content=payload, from_client=True, is_text=True
                    )
                ]
            ),
        )
        capture.websocket_message(flow)

        events = _drain(out_q)
        frame = next(
            e for e in events if isinstance(e, NetworkWebSocketFrameEvent)
        )
        assert frame.body_ciphertext is not None
        assert frame.body_nonce is not None and len(frame.body_nonce) == 12
        assert frame.body_aad is not None

        plaintext = _crypto.decrypt_body(
            frame.body_ciphertext,
            frame.body_nonce,
            self._TEST_DEK,
            frame.body_aad,
        )
        assert plaintext == payload

    def test_aad_consistency_with_event_fields(self):
        # The AAD persisted on the event (body_aad) MUST equal the AAD
        # reconstructed from (recording_id, flow_id, type, timestamp_ns)
        # via crypto.aad_bytes — that's the "single source of truth"
        # invariant the export-time scrubber relies on.
        from screencap.engine.events import EventType
        from screencap.network import crypto as _crypto

        body = b"AAD-consistency-check-payload"
        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
        )
        flow = FakeFlow(
            id="aad-flow-007",
            request=FakeRequest(
                method="POST",
                host="example.com",
                headers=FakeHeaders([("Content-Length", str(len(body)))]),
                raw_content=body,
                content=body,
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
                raw_content=b"",
                content=b"",
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        req = next(e for e in events if isinstance(e, NetworkRequestEvent))

        # Reconstruct AAD from the event's own fields. recording_id is
        # 42 (from _make_capture) and the type is the request enum.
        reconstructed = _crypto.aad_bytes(
            recording_id=42,
            flow_id=req.flow_id,
            event_type=EventType.NETWORK_REQUEST.value,
            ts_ns=req.timestamp_ns,
        )
        assert reconstructed == req.body_aad

        # And the reconstructed AAD must decrypt the persisted ciphertext.
        plaintext = _crypto.decrypt_body(
            req.body_ciphertext, req.body_nonce, self._TEST_DEK, reconstructed
        )
        assert plaintext == body

    def test_encrypt_failure_falls_through_to_metadata(
        self, monkeypatch, tmp_path
    ):
        # Patch crypto.encrypt_body to raise — the addon MUST log the
        # failure and emit a metadata-only event without crashing.
        from screencap.network import capture_addon as _capture_addon_mod

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated encryption failure")

        monkeypatch.setattr(
            _capture_addon_mod._crypto, "encrypt_body", boom
        )

        log_path = tmp_path / "log.txt"
        body = b'{"hello":"world"}'
        capture, out_q = _make_capture(
            dek=self._TEST_DEK,
            capture_bodies_for={"example.com"},
            override_default_capture_bodies_for=True,
            log_path=log_path,
        )
        flow = FakeFlow(
            id="boom-flow",
            request=FakeRequest(
                method="POST",
                host="example.com",
                headers=FakeHeaders([("Content-Length", str(len(body)))]),
                raw_content=body,
                content=body,
            ),
            response=FakeResponse(
                status_code=200,
                headers=FakeHeaders([("Content-Length", "0")]),
                raw_content=b"",
                content=b"",
            ),
        )
        capture.requestheaders(flow)
        capture.request(flow)  # MUST NOT raise
        capture.responseheaders(flow)
        capture.response(flow)

        events = _drain(out_q)
        req = next(e for e in events if isinstance(e, NetworkRequestEvent))
        assert req.body_ciphertext is None
        assert req.body_nonce is None
        assert req.body_aad is None
        # Failure logged.
        assert "body-encrypt failed" in log_path.read_text()
