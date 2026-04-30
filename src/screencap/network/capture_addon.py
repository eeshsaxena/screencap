"""Mitmproxy capture addon (V1 — metadata-only; V1.5 — encrypted bodies).

The :class:`NetworkCapture` addon hooks the standard mitmproxy lifecycle
events and pushes Pydantic-shaped metadata events onto a
``multiprocessing.Queue`` consumed by the main recorder. **V1 has NO
body retention and NO encryption**: every body is hashed (over WIRE
bytes) and the bytes themselves are discarded. Only the size + sha256
hex digest survive.

V1.5 body capture
-----------------
When the addon is constructed with a non-None ``dek`` and the host is
in the effective body-capture allowlist (see
:func:`screencap.network.blocklist.is_host_in_capture_bodies_for`),
``request()`` / ``response()`` / ``websocket_message()`` ALSO encrypt
the DECODED body (``flow.{request,response}.content`` — the gunzipped
form) under AES-256-GCM bound to a per-event AAD constructed by
:func:`screencap.network.crypto.aad_bytes`. The ciphertext, nonce and
AAD are populated on the emitted Pydantic event (``body_ciphertext`` /
``body_nonce`` / ``body_aad``).

The V1 wire-bytes hashing path (``_finalize_body_hash``,
``make_hash_transformer``, ``_should_stream``) is UNCHANGED — body_size
and body_sha256_hex remain wire-byte values per the V1 schema lock-in.

Decoded vs wire: a server with ``Content-Encoding: gzip`` and
``Content-Length: 5KB`` could deliver a 200KB decoded body. The wire
size passed the streaming-decision cap, but the storage cost is the
decoded size. We therefore re-check ``len(decoded) <= body_size_cap``
before encrypting; bodies whose decoded form exceeds the cap stay
metadata-only. Encrypting decoded bytes also lets the export-time
scrubber operate against plaintext directly without gunzipping.

Streaming bodies (large or chunked) stay metadata-only in V1.5
regardless of the host allowlist: buffering up to ``body_size_cap``
bytes inside the chunk-hashing transformer would defeat the point of
streaming (one of the reasons we stream is precisely to avoid such
buffering).

Concurrency model
-----------------
Mitmproxy 11.x runs all addon hooks AND stream transformers on the
master's single asyncio loop, sequential within that loop. The
``self._stream_state`` dict is therefore not under multi-thread access
in normal operation. We capture ``self._loop`` in :meth:`running` and
soft-assert single-loop in the body-hash path (the assertion only fires
if a future mitmproxy version, third-party addon, or threading hook
breaks the single-loop invariant; failure-open is safer than silent
races). If the assertion ever fails, wrap state mutations in
``threading.Lock()`` (cheap; uncontended in single-loop mode).

Hook flow shape
---------------
1. ``requestheaders`` — first gate for blocked hosts; install streaming
   transformer if request body exceeds cap or has unknown size.
2. ``request`` — emit :class:`NetworkRequestEvent` with hashed-and-
   discarded body metadata.
3. ``responseheaders`` — install streaming transformer for response if
   needed.
4. ``response`` — emit :class:`NetworkResponseEvent` (suppressed for
   WebSocket 101 upgrades; the upgrade event covers it).
5. ``websocket_start`` — emit :class:`NetworkWebSocketUpgradeEvent`.
6. ``websocket_message`` — emit :class:`NetworkWebSocketFrameEvent`,
   then unconditionally pop the message from
   ``flow.websocket.messages`` to bound memory.
7. ``tls_clienthello`` — opt-out of MITM for hosts in
   ``runtime_tunnel_hosts`` (cert-pinned hosts that previously failed).
8. ``error`` — classify TLS-pinning failures and emit a
   :class:`NetworkPinFailureEvent` (control-only sideband, NOT a
   ``BaseEvent``).
9. ``done`` — final drop-burst flush, log final stats.

Drop-burst path
---------------
Both ``out_q.put_nowait`` (regular events) and a per-second timer +
shutdown synthesize :class:`NetworkDropBurstEvent` markers when events
are dropped (``source="addon"`` in ``details_json``). Drop-with-
explicit-marker is the locked policy: silent drops bias the captured
data invisibly.
"""

from __future__ import annotations

import asyncio
import hashlib
import queue
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from screencap.engine.convert import bytes_to_hex
from screencap.engine.events import (
    EventType,
    NetworkDropBurstEvent,
    NetworkPinFailureEvent,
    NetworkRequestEvent,
    NetworkResponseEvent,
    NetworkTunneledEvent,
    NetworkWebSocketFrameEvent,
    NetworkWebSocketUpgradeEvent,
)
from screencap.network import crypto as _crypto
from screencap.network.blocklist import (
    is_host_blocked,
    is_host_in_capture_bodies_for,
)
from screencap.network.redaction import redact_headers, redact_url_query

if TYPE_CHECKING:
    import multiprocessing as mp

    from screencap.network.config import NetworkConfig
    from screencap.privacy.policy import PrivacyConfig


# TLS handshake / pinning failure indicators in mitmproxy's flow.error.msg.
# We match against lowercase substrings; the live error strings contain
# version / cipher detail we don't want to bind to.
_TLS_PIN_INDICATORS: tuple[str, ...] = (
    "tlsv1 alert certificate unknown",
    "ssl handshake failure",
    "tls handshake error",
    "tlsv1 alert unknown ca",
    "certificate verify failed",
    "ssl: certificate_verify_failed",
)


def make_hash_transformer(
    flow_id: str,
    direction: str,
    state: dict[str, dict[str, Any]],
) -> Callable[[bytes], bytes]:
    """Return a per-chunk transformer for mitmproxy ``flow.{request,response}.stream``.

    Updates a running ``hashlib.sha256`` + body-size counter in
    ``state[flow_id]``; returns each chunk **unchanged** (we never
    modify wire bytes). The terminal call (``data == b""``) finalizes
    the hash.

    Args:
        flow_id: ``flow.id`` (uuid4 hex).
        direction: ``"req"`` or ``"resp"`` — used as the key prefix in
            ``state[flow_id]``.
        state: shared dict keyed by ``flow.id``. The transformer will
            call ``state.setdefault(flow_id, {})`` on first chunk.

    Returns:
        ``hash_chunk(data: bytes) -> bytes`` ready for ``flow.stream =``.
    """
    sha = hashlib.sha256()
    size = 0

    def hash_chunk(data: bytes) -> bytes:
        nonlocal size
        if data == b"":
            # End-of-stream marker — finalize the hash.
            entry = state.setdefault(flow_id, {})
            entry[f"{direction}_sha"] = sha.digest()
            entry[f"{direction}_size"] = size
            return b""
        sha.update(data)
        size += len(data)
        return data

    return hash_chunk


def _log(log_path: Path | None, msg: str) -> None:
    """Append a line to the diagnostic log file (best-effort)."""
    if log_path is None:
        return
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.time():.3f}] {msg}\n")
    except OSError:
        # Diagnostic log can't take the recorder down.
        pass


def _headers_to_list(headers_obj: Any) -> list[tuple[str, str]]:
    """Convert a mitmproxy ``Headers`` object to ``list[tuple[str, str]]``.

    Tries ``items(multi=True)`` first; falls back to iterating
    ``.fields`` (raw bytes) for stubs that only expose the lower-level
    shape.
    """
    if headers_obj is None:
        return []

    items = getattr(headers_obj, "items", None)
    if callable(items):
        try:
            return [(name, value) for name, value in items(multi=True)]
        except TypeError:
            pass

    fields = getattr(headers_obj, "fields", None)
    if fields is not None:
        out: list[tuple[str, str]] = []
        for name, value in fields:
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            out.append((str(name), str(value)))
        return out

    if isinstance(headers_obj, list):
        return list(headers_obj)

    return []


def _is_tls_pin_failure(msg: str) -> bool:
    """Return True iff the error message matches a TLS pin / handshake fail."""
    if not msg:
        return False
    lowered = msg.lower()
    return any(indicator in lowered for indicator in _TLS_PIN_INDICATORS)


class NetworkCapture:
    """Mitmproxy addon — emits metadata-only network events to ``out_q``.

    The class is importable without mitmproxy installed (we duck-type
    flow / message arguments). Mitmproxy hooks call methods by name; we
    keep them as documented in the module docstring.
    """

    def __init__(
        self,
        out_q: "mp.Queue",
        recording_id: int,
        network_config: "NetworkConfig",
        privacy_config: "PrivacyConfig",
        log_path: Path | None,
        *,
        dek: bytes | None = None,
    ) -> None:
        """Construct a capture addon.

        Args:
            out_q: ``multiprocessing.Queue`` (or duck-type) the addon
                pushes events onto.
            recording_id: integer identifier for the active recording
                (used as the ``r`` field of every per-event AAD; see
                :func:`screencap.network.crypto.aad_bytes`).
            network_config: parsed ``[network]`` config table.
            privacy_config: parsed ``[privacy]`` config table.
            log_path: diagnostic log file (best-effort writes; ``None``
                disables logging).
            dek: V1.5 per-recording Data Encryption Key (32 bytes).
                When ``None`` (default — preserves V1 callers and tests
                that don't supply one), the body-encryption branches
                are SKIPPED entirely and the addon emits metadata-only
                events exactly as in V1. When set, bodies for hosts in
                the effective allowlist (and within the size cap, on
                non-streamed flows) are encrypted with this key under
                AES-256-GCM and emitted on the
                ``body_ciphertext`` / ``body_nonce`` / ``body_aad``
                fields of the Pydantic event.
        """
        self._out_q = out_q
        self._recording_id = recording_id
        self._network_config = network_config
        self._privacy_config = privacy_config
        self._log_path = log_path
        self._dek = dek

        self._stream_state: dict[str, dict[str, Any]] = {}
        self._dropped_count: int = 0
        self._dropped_hosts: set[str] = set()
        self._dropped_window_start_ns: int | None = None
        # V1.5: seed runtime_tunnel_hosts with previously-detected pinned
        # hosts so a host that failed in recording N never fails again in
        # recording N+1. Best-effort load — file IO errors give an empty
        # set (V1 behavior preserved).
        from screencap.network import pinned_hosts as _pin  # noqa: PLC0415
        try:
            self._runtime_tunnel_hosts: set[str] = _pin.load_known_pinned_hosts()
        except Exception:  # noqa: BLE001 — pre-loaded cache is best-effort
            self._runtime_tunnel_hosts = set()
        # V1.5: hosts ACTUALLY OBSERVED as tunneled during this recording
        # (subset of ``_runtime_tunnel_hosts``). Populated from
        # ``tls_clienthello`` (when ``ignore_connection`` fires) and
        # ``error()`` (mid-recording new pin failure). ``done()`` emits
        # one ``network.tunneled`` event per host in THIS set, NOT the
        # cache — otherwise every recording would emit bogus tunneled
        # events for every host the user has ever encountered.
        self._observed_tunnel_hosts: set[str] = set()
        # Track each host's first-observed-this-recording timestamp so
        # the network.tunneled event has accurate started_at.
        self._tunnel_started_at: dict[str, float] = {}
        self._pin_failure_emitted: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drop_burst_timer_handle: asyncio.TimerHandle | None = None
        self._flow_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def running(self) -> None:
        """Capture the asyncio loop and start the drop-burst timer."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop running (test path) — drop-burst tick is then
            # driven manually by tests.
            self._loop = None
        if self._loop is not None:
            self._drop_burst_timer_handle = self._loop.call_later(
                1.0, self._tick_drop_burst
            )

    def done(self) -> None:
        """Flush final drop burst and log shutdown stats."""
        if self._drop_burst_timer_handle is not None:
            try:
                self._drop_burst_timer_handle.cancel()
            except Exception:
                pass
            self._drop_burst_timer_handle = None

        flushed_count = self._dropped_count
        if flushed_count > 0:
            self._emit_drop_burst(blocking=True)

        # V1.5 task 11: emit one network.tunneled event per host that was
        # ACTUALLY OBSERVED as tunneled during this recording. Iterates
        # ``_observed_tunnel_hosts`` (populated by tls_clienthello and
        # error()) rather than ``_runtime_tunnel_hosts`` (which would
        # include every host the user has ever pinned across all past
        # recordings — a bogus signal). Lets the training pipeline mark
        # API-not-observable time spans rather than silently treating
        # them as no-traffic windows.
        now = time.time()
        for host in sorted(self._observed_tunnel_hosts):
            started_at = self._tunnel_started_at.get(host, now)
            duration = max(0.0, now - started_at)
            try:
                tunneled_event = NetworkTunneledEvent(
                    timestamp=now,
                    timestamp_ns=time.time_ns(),
                    host=host,
                    started_at=started_at,
                    duration_seconds=duration,
                )
                self._enqueue(tunneled_event, host=host)
            except Exception:  # noqa: BLE001
                _log(self._log_path, f"failed to emit network.tunneled for {host}")

        _log(
            self._log_path,
            f"shutdown: flows={self._flow_count} "
            f"final_drop_count={flushed_count} "
            f"runtime_tunnel_hosts={sorted(self._runtime_tunnel_hosts)}",
        )

    # ------------------------------------------------------------------
    # HTTP request hooks
    # ------------------------------------------------------------------

    def requestheaders(self, flow: Any) -> None:
        """First chance to gate by host and install request streaming."""
        try:
            host = flow.request.host or ""
        except AttributeError:
            return

        if is_host_blocked(host, self._privacy_config, self._network_config):
            flow.metadata["screencap_blocked"] = True
            _log(self._log_path, f"blocked HTTP host (request): {host}")
            return

        # Detect WebSocket upgrade intent so the regular response()
        # hook can suppress the 101 emission. websocket_start() will
        # emit the upgrade event.
        upgrade = ""
        try:
            upgrade = flow.request.headers.get("upgrade", "") or ""
        except AttributeError:
            upgrade = ""
        if upgrade and "websocket" in upgrade.lower():
            flow.metadata["screencap_ws_upgrade_pending"] = True

        if self._should_stream(flow.request):
            flow.request.stream = make_hash_transformer(
                flow.id, "req", self._stream_state
            )

    def request(self, flow: Any) -> None:
        """Emit :class:`NetworkRequestEvent` after the request body is available."""
        if flow.metadata.get("screencap_blocked"):
            return

        try:
            self._flow_count += 1
            # Single ts_ns source — the AAD MUST match the event's
            # timestamp_ns or decrypt will raise InvalidTag at export.
            ts_ns = time.time_ns()
            ts = ts_ns / 1e9

            headers = redact_headers(_headers_to_list(flow.request.headers))
            url = redact_url_query(flow.request.url)
            body_size, body_sha_hex = self._finalize_body_hash(
                flow, "req", flow.request
            )
            body_ciphertext, body_nonce, body_aad = self._maybe_encrypt_body(
                flow,
                flow.request,
                direction="req",
                event_type=EventType.NETWORK_REQUEST.value,
                ts_ns=ts_ns,
            )

            event = NetworkRequestEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                flow_id=flow.id,
                method=flow.request.method,
                url=url,
                host=flow.request.host,
                headers=headers,
                body_size=body_size,
                body_sha256_hex=body_sha_hex,
                content_type=flow.request.headers.get("content-type"),
                http_version=getattr(flow.request, "http_version", None),
                body_ciphertext=body_ciphertext,
                body_nonce=body_nonce,
                body_aad=body_aad,
            )
            self._enqueue(event, host=flow.request.host)
        finally:
            # Pop only the request-side state. Response-side state (if
            # any) is popped after response().
            entry = self._stream_state.get(flow.id)
            if entry is not None:
                entry.pop("req_sha", None)
                entry.pop("req_size", None)
                if not entry:
                    self._stream_state.pop(flow.id, None)

    def responseheaders(self, flow: Any) -> None:
        """Install response streaming transformer if needed."""
        if flow.metadata.get("screencap_blocked"):
            return
        if flow.response is None:
            return

        if self._should_stream(flow.response):
            flow.response.stream = make_hash_transformer(
                flow.id, "resp", self._stream_state
            )

    def response(self, flow: Any) -> None:
        """Emit :class:`NetworkResponseEvent` (or suppress for WS 101)."""
        if flow.metadata.get("screencap_blocked"):
            return

        # WebSocket upgrade — suppress the 101 emission; upgrade event
        # covers it. Still pop the stream state defensively.
        if flow.metadata.get("screencap_ws_upgrade_pending"):
            self._stream_state.pop(flow.id, None)
            return

        if flow.response is None:
            self._stream_state.pop(flow.id, None)
            return

        try:
            ts_ns = time.time_ns()
            ts = ts_ns / 1e9

            headers = redact_headers(_headers_to_list(flow.response.headers))
            body_size, body_sha_hex = self._finalize_body_hash(
                flow, "resp", flow.response
            )
            body_ciphertext, body_nonce, body_aad = self._maybe_encrypt_body(
                flow,
                flow.response,
                direction="resp",
                event_type=EventType.NETWORK_RESPONSE.value,
                ts_ns=ts_ns,
            )

            event = NetworkResponseEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                flow_id=flow.id,
                host=flow.request.host,
                status=flow.response.status_code,
                headers=headers,
                body_size=body_size,
                body_sha256_hex=body_sha_hex,
                content_type=flow.response.headers.get("content-type"),
                http_version=getattr(flow.response, "http_version", None),
                body_ciphertext=body_ciphertext,
                body_nonce=body_nonce,
                body_aad=body_aad,
            )
            self._enqueue(event, host=flow.request.host)
        finally:
            self._stream_state.pop(flow.id, None)

    # ------------------------------------------------------------------
    # WebSocket hooks
    # ------------------------------------------------------------------

    def websocket_start(self, flow: Any) -> None:
        """Emit :class:`NetworkWebSocketUpgradeEvent` after the 101 handshake."""
        if flow.metadata.get("screencap_blocked"):
            return

        try:
            request_headers = redact_headers(
                _headers_to_list(flow.request.headers)
            )
            response_headers: list[tuple[str, str]] = []
            status = 101
            http_version: str | None = None
            if flow.response is not None:
                response_headers = redact_headers(
                    _headers_to_list(flow.response.headers)
                )
                status = flow.response.status_code
                http_version = getattr(flow.response, "http_version", None)

            event = NetworkWebSocketUpgradeEvent(
                timestamp=time.time(),
                timestamp_ns=time.time_ns(),
                flow_id=flow.id,
                url=redact_url_query(flow.request.url),
                host=flow.request.host,
                status=status,
                headers=response_headers,
                http_version=http_version,
                details_json={
                    "request_headers": [list(h) for h in request_headers]
                },
            )
            self._enqueue(event, host=flow.request.host)
        except Exception as exc:  # noqa: BLE001 — log + drop, never crash
            _log(self._log_path, f"websocket_start error: {exc!r}")

    def websocket_message(self, flow: Any) -> None:
        """Emit :class:`NetworkWebSocketFrameEvent` for the latest frame.

        **CRITICAL:** the message is popped from
        ``flow.websocket.messages`` UNCONDITIONALLY after the put
        attempt — otherwise dropped frames pile up on long-lived
        sockets and leak memory.
        """
        if flow.metadata.get("screencap_blocked"):
            try:
                flow.websocket.messages.pop(-1)
            except (AttributeError, IndexError):
                pass
            return

        try:
            try:
                msg = flow.websocket.messages[-1]
            except (AttributeError, IndexError):
                return

            ts_ns = time.time_ns()
            ts = ts_ns / 1e9

            content = msg.content or b""
            body_size = len(content)
            body_sha_hex = bytes_to_hex(hashlib.sha256(content).digest())
            direction = "sent" if msg.from_client else "received"
            frame_type = "text" if msg.is_text else "binary"
            body_ciphertext, body_nonce, body_aad = self._maybe_encrypt_body(
                flow,
                msg,
                direction="ws",  # ignored for WS
                event_type=EventType.NETWORK_WS_FRAME.value,
                ts_ns=ts_ns,
                is_websocket=True,
            )

            event = NetworkWebSocketFrameEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                flow_id=flow.id,
                host=flow.request.host,
                direction=direction,
                frame_type=frame_type,
                body_size=body_size,
                body_sha256_hex=body_sha_hex,
                body_ciphertext=body_ciphertext,
                body_nonce=body_nonce,
                body_aad=body_aad,
            )
            self._enqueue(event, host=flow.request.host)
        finally:
            # Unconditional pop — drops or successes both pop.
            try:
                flow.websocket.messages.pop(-1)
            except (AttributeError, IndexError):
                pass

    # ------------------------------------------------------------------
    # TLS / error hooks
    # ------------------------------------------------------------------

    def tls_clienthello(self, data: Any) -> None:
        """If the SNI is in our runtime-tunnel set, opt-out of MITM.

        DNS / SNI hostnames are case-insensitive per RFC 6066, so the
        membership check MUST lowercase the SNI before comparing —
        the cache stores lowercased entries (per
        ``pinned_hosts.load_known_pinned_hosts``). Without this,
        a client sending ``Pinned.Example.com`` would miss
        ``pinned.example.com`` in the cache, the addon would attempt
        MITM, the cert pin would fail, and the persistent cache that
        Unit 10 added would be defeated for any mixed-case SNI.
        """
        try:
            sni = data.client_hello.sni
        except AttributeError:
            return
        if not sni:
            return
        sni_lc = sni.lower()
        if sni_lc in self._runtime_tunnel_hosts:
            data.ignore_connection = True
            # Mark observed-this-recording so done() emits a
            # network.tunneled event covering the actual tunneled span.
            if sni_lc not in self._observed_tunnel_hosts:
                self._observed_tunnel_hosts.add(sni_lc)
                self._tunnel_started_at.setdefault(sni_lc, time.time())

    def error(self, flow: Any) -> None:
        """Classify TLS-pinning failures, emit one-time pin notice.

        Always pops per-flow stream state to prevent leaks if the flow
        errored mid-stream.
        """
        try:
            err_msg = ""
            if getattr(flow, "error", None) is not None:
                err_msg = str(flow.error.msg or "")

            host = ""
            try:
                host = flow.request.host or ""
            except AttributeError:
                pass

            _log(self._log_path, f"flow error host={host} msg={err_msg!r}")

            if host and _is_tls_pin_failure(err_msg):
                host_lc = host.lower()
                if host_lc not in self._runtime_tunnel_hosts:
                    self._runtime_tunnel_hosts.add(host_lc)
                    # V1.5: persist for the next recording so this host
                    # never fails its first connection again. Best-effort.
                    try:
                        from screencap.network import (  # noqa: PLC0415
                            pinned_hosts as _pin,
                        )
                        _pin.add_known_pinned_host(host_lc)
                    except Exception:  # noqa: BLE001
                        _log(
                            self._log_path,
                            f"failed to persist pinned host {host_lc}",
                        )
                # Mark observed-this-recording (whether the host is
                # newly-detected or was pre-loaded from cache and just
                # failed for the first time this recording). done()
                # uses _observed_tunnel_hosts to emit network.tunneled.
                if host_lc not in self._observed_tunnel_hosts:
                    self._observed_tunnel_hosts.add(host_lc)
                    self._tunnel_started_at.setdefault(host_lc, time.time())
                if host not in self._pin_failure_emitted:
                    self._pin_failure_emitted.add(host)
                    pin_event = NetworkPinFailureEvent(
                        timestamp=time.time(),
                        timestamp_ns=time.time_ns(),
                        host=host,
                        reason="tls_pinning_or_handshake_failure",
                    )
                    self._enqueue(pin_event, host=host)
        finally:
            self._stream_state.pop(flow.id, None)

    # ------------------------------------------------------------------
    # Helpers (private)
    # ------------------------------------------------------------------

    def _should_stream(self, msg: Any) -> bool:
        """Decide whether to install the chunk-hashing transformer."""
        try:
            content_length = msg.headers.get("content-length")
            transfer_encoding = msg.headers.get("transfer-encoding", "") or ""
        except AttributeError:
            return True

        if transfer_encoding and "chunked" in transfer_encoding.lower():
            return True
        if content_length is None:
            return True
        try:
            length = int(content_length)
        except (TypeError, ValueError):
            return True
        return length > self._network_config.body_size_cap

    def _finalize_body_hash(
        self,
        flow: Any,
        direction: str,
        msg: Any,
    ) -> tuple[int, str | None]:
        """Return ``(body_size, body_sha256_hex)`` for a request or response.

        If the streamed transformer was installed, read the finalized
        hash + size from ``self._stream_state``. Otherwise compute over
        wire bytes (``raw_content``, NEVER ``content`` which is decoded).
        """
        # Single-loop assertion (defense in depth) — log only, don't raise.
        if self._loop is not None:
            try:
                running_loop = asyncio.get_running_loop()
                if running_loop is not self._loop:
                    _log(
                        self._log_path,
                        "WARN: addon hook on different loop than running()",
                    )
            except RuntimeError:
                pass

        entry = self._stream_state.get(flow.id, {})
        sha_key = f"{direction}_sha"
        size_key = f"{direction}_size"
        if sha_key in entry:
            return entry[size_key], bytes_to_hex(entry[sha_key])

        wire = getattr(msg, "raw_content", None)
        if wire is None:
            return 0, None
        body_size = len(wire)
        body_sha = hashlib.sha256(wire).digest()
        return body_size, bytes_to_hex(body_sha)

    def _maybe_encrypt_body(
        self,
        flow: Any,
        msg: Any,
        *,
        direction: str,
        event_type: str,
        ts_ns: int,
        is_websocket: bool = False,
    ) -> tuple[bytes | None, bytes | None, bytes | None]:
        """Encrypt the decoded body for V1.5 body-capture flows.

        Returns ``(ciphertext, nonce, aad)`` when body capture is
        engaged for this message, or ``(None, None, None)`` to fall
        through to V1 metadata-only behavior.

        Skipped (returns triple-None) when:
            * ``self._dek is None`` -- V1 caller / no encryption
              configured.
            * Host not in the effective capture-bodies-for allowlist
              (this also enforces the blocklist-wins-over-allowlist
              precedence; see
              :func:`screencap.network.blocklist.is_host_in_capture_bodies_for`).
            * Streaming was engaged on this direction -- the chunk-
              hashing transformer ran over wire bytes, and we don't
              buffer ``body_size_cap`` worth of chunks just to encrypt
              them. Detected via the per-flow stream-state entry
              populated by :func:`make_hash_transformer`.
            * Decoded body size exceeds ``body_size_cap`` -- the
              compressed-bypass guard documented in the module
              docstring.
            * No decodable content available (``content`` attribute
              missing or ``None``).

        For WebSocket frames (``is_websocket=True``), there is no
        decoded/wire distinction (WS frames are not gzipped by the
        protocol), so the size check uses ``len(msg.content)`` directly
        and the streaming-state check is skipped (WS messages are not
        stream-transformed).

        Any unexpected exception raised inside the encryption path is
        caught and logged; the function returns triple-None so the
        emitted event falls through to metadata-only. Body encryption
        must NEVER take down the proxy hot path.

        Args:
            flow: the mitmproxy flow (used for ``flow.id`` and host).
            msg: ``flow.request`` / ``flow.response`` / a
                ``WebSocketMessage``.
            direction: ``"req"`` / ``"resp"`` for HTTP, ignored for WS.
            event_type: the ``EventType`` value being emitted (e.g.
                ``"network.request"``) -- bound into the AAD.
            ts_ns: the SAME ``timestamp_ns`` value being assigned to
                the emitted event. Critical: AAD-time and event-time
                ``ts_ns`` MUST agree or decryption fails at export.
            is_websocket: True iff ``msg`` is a WS frame message.
        """
        if self._dek is None:
            return None, None, None

        try:
            host = flow.request.host or ""
        except AttributeError:
            return None, None, None

        if not is_host_in_capture_bodies_for(
            host, self._privacy_config, self._network_config
        ):
            return None, None, None

        # Streaming check: HTTP only -- WS messages are never stream-
        # transformed. The stream-state entry survives until response()
        # / request() pops it; we read it here BEFORE that pop.
        if not is_websocket:
            entry = self._stream_state.get(flow.id, {})
            if f"{direction}_sha" in entry:
                # Streaming engaged -- hash done over wire chunks; we
                # don't buffer to encrypt.
                return None, None, None

        try:
            decoded = getattr(msg, "content", None)
        except Exception:  # noqa: BLE001 -- mitmproxy quirks shouldn't crash
            decoded = None
        if decoded is None:
            return None, None, None

        # Decoded-size cap re-check (compressed-bypass guard).
        if len(decoded) > self._network_config.body_size_cap:
            return None, None, None

        try:
            aad = _crypto.aad_bytes(
                recording_id=self._recording_id,
                flow_id=flow.id,
                event_type=event_type,
                ts_ns=ts_ns,
            )
            ciphertext, nonce = _crypto.encrypt_body(
                self._dek, decoded, aad
            )
        except Exception as exc:  # noqa: BLE001 -- log + fall through
            _log(
                self._log_path,
                f"body-encrypt failed flow={flow.id} type={event_type} "
                f"err={exc!r}",
            )
            return None, None, None

        return ciphertext, nonce, aad

    def _enqueue(self, event: Any, *, host: str) -> None:
        """Push to ``out_q`` non-blocking; on full, account a drop burst."""
        try:
            self._out_q.put_nowait(event)
        except queue.Full:
            self._record_drop(host)

    def _record_drop(self, host: str) -> None:
        """Increment the drop counter and remember the host."""
        self._dropped_count += 1
        if host:
            self._dropped_hosts.add(host)
        if self._dropped_window_start_ns is None:
            self._dropped_window_start_ns = time.time_ns()

    def _tick_drop_burst(self) -> None:
        """Per-second timer callback — flushes any pending drops."""
        if self._dropped_count > 0:
            self._emit_drop_burst(blocking=False)
        if self._loop is not None:
            self._drop_burst_timer_handle = self._loop.call_later(
                1.0, self._tick_drop_burst
            )

    def _emit_drop_burst(self, *, blocking: bool) -> None:
        """Synthesize and emit a :class:`NetworkDropBurstEvent`."""
        start_ns = self._dropped_window_start_ns or time.time_ns()
        event = NetworkDropBurstEvent(
            timestamp=start_ns / 1e9,
            timestamp_ns=start_ns,
            details_json={
                "dropped_count": self._dropped_count,
                "hosts_affected": sorted(self._dropped_hosts),
                "source": "addon",
            },
        )
        timeout = 1.0 if blocking else 0.1
        try:
            self._out_q.put(event, timeout=timeout)
        except queue.Full:
            _log(
                self._log_path,
                f"FATAL: drop_burst put timed out (count={self._dropped_count})",
            )
        finally:
            self._dropped_count = 0
            self._dropped_hosts.clear()
            self._dropped_window_start_ns = None
