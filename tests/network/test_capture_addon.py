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
