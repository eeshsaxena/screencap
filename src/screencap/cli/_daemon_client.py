"""HTTP-over-AF_UNIX client used by CLI live-state commands.

Phase 2 U1 collapses ``screencap start``/``stop``/``status`` from
in-process engine spawn and lockfile-based state reads down to thin
HTTP clients of the Phase 1 daemon API. This module is the Python-side
analogue of the SwiftUI ``DaemonClient.swift`` brought up in Phase 1 U7
— both wrap ``httpx`` transports configured against
``~/.screencap/run/api.sock``.

Kept narrow on purpose: no auto-spawn here (see ``_autospawn``), no
retry policy beyond what callers explicitly opt into, no JSON envelope
normalization. The CLI command bodies decide how to format errors
against the existing rich Console UX.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

# Pin the daemon API schema version the CLI was compiled against. A
# response carrying a different ``api_schema_version`` triggers a clear
# kickstart-the-daemon message rather than silent shape drift; the
# daemon owns forward compatibility, the CLI does not negotiate.
SUPPORTED_API_SCHEMA_VERSION = 1


class DaemonUnreachableError(RuntimeError):
    """The daemon socket is missing, refused, or hung up unexpectedly.

    Distinct from ``DaemonClientError`` because callers may want to
    auto-spawn or surface a kickstart hint instead of treating it as a
    contract failure.
    """


class DaemonClientError(RuntimeError):
    """The daemon returned an error envelope; ``envelope`` carries the body.

    Named ``DaemonClientError`` (not ``DaemonAPIError``) to avoid a
    collision with ``screencap.daemon.errors.DaemonAPIError``, which is
    the server-side base exception class.
    """

    def __init__(self, envelope: dict[str, Any], *, status_code: int) -> None:
        self.envelope = envelope
        self.status_code = status_code
        code = envelope.get("error") or envelope.get("error_code") or "unknown"
        super().__init__(f"daemon error: {code} (status={status_code})")


# Backward-compatible alias so existing callers importing ``DaemonAPIError``
# from this module continue to work without immediate churn.
DaemonAPIError = DaemonClientError


class SchemaMismatchError(RuntimeError):
    """The daemon advertises an api_schema_version the CLI does not recognize.

    Surfaces to the user with kickstart guidance; the CLI does not
    attempt to interpret an unknown contract.
    """

    def __init__(self, daemon_version: int) -> None:
        self.daemon_version = daemon_version
        if daemon_version < 0:
            super().__init__(
                f"daemon did not advertise api_schema_version (old daemon?); "
                f"CLI supports {SUPPORTED_API_SCHEMA_VERSION}"
            )
        else:
            super().__init__(
                f"daemon advertises api_schema_version={daemon_version}, "
                f"CLI supports {SUPPORTED_API_SCHEMA_VERSION}"
            )


class DaemonHTTPClient:
    """Synchronous HTTP-over-AF_UNIX client for the daemon's ``/v0/*`` surface.

    Built per-invocation. The underlying ``httpx.Client`` is opened
    lazily on first call and reused for the lifetime of the instance.
    Live-state CLI commands construct one client per invocation, so
    no shared-pool concerns apply.
    """

    def __init__(
        self,
        socket_path: Path | str | None = None,
        *,
        connect_timeout: float = 2.0,
        read_timeout: float | None = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if socket_path:
            self._socket_path = Path(socket_path).expanduser()
        else:
            from screencap.daemon.socket import default_socket_path
            self._socket_path = default_socket_path()
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        # ``transport`` is injectable so tests can drop in a
        # ``MockTransport`` or ``ASGITransport`` without binding a real
        # UNIX socket. Production code never passes it.
        self._explicit_transport = transport
        self._client: httpx.Client | None = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def _build_client(self) -> httpx.Client:
        transport = self._explicit_transport
        if transport is None:
            transport = httpx.HTTPTransport(uds=str(self._socket_path))
        return httpx.Client(
            base_url="http://daemon",
            transport=transport,
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=self._read_timeout,
                write=self._read_timeout,
                pool=self._connect_timeout,
            ),
            trust_env=False,
        )

    def _client_or_open(self) -> httpx.Client:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> DaemonHTTPClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- low-level request helpers ------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        stream: bool = False,
        params: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        client = self._client_or_open()
        try:
            if stream:
                # Callers using ``stream=True`` must consume + close the
                # response themselves via ``events()``. Returning the raw
                # response here keeps the streaming-iter contract single-
                # sited.
                req = client.build_request(
                    method, path, json=json_body, params=params, timeout=timeout
                )
                return client.send(req, stream=True)
            return client.request(
                method,
                path,
                json=json_body,
                params=params,
                timeout=timeout,
            )
        except httpx.ConnectError as exc:
            raise DaemonUnreachableError(
                f"could not connect to daemon at {self._socket_path}: {exc}"
            ) from exc
        except httpx.ReadError as exc:
            raise DaemonUnreachableError(
                f"daemon disconnected mid-response: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise DaemonUnreachableError(
                f"daemon timed out: {exc}"
            ) from exc
        except httpx.RemoteProtocolError as exc:
            raise DaemonUnreachableError(
                f"daemon returned invalid HTTP response: {exc}"
            ) from exc

    @staticmethod
    def _decode_envelope(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise DaemonUnreachableError(
                f"daemon returned non-JSON body (status={response.status_code})"
            ) from exc
        if not isinstance(data, dict):
            raise DaemonUnreachableError(
                f"daemon returned non-object JSON body (status={response.status_code})"
            )
        return data

    def _check_schema_or_raise(self, envelope: dict[str, Any]) -> None:
        version = envelope.get("api_schema_version")
        if version is None:
            # Missing field — old daemon that predates api_schema_version.
            raise SchemaMismatchError(-1)
        if not isinstance(version, int):
            # String-typed or unexpected shape; treat as incompatible.
            raise SchemaMismatchError(-1)
        if version != SUPPORTED_API_SCHEMA_VERSION:
            raise SchemaMismatchError(version)

    def _parse_ok_envelope(self, response: httpx.Response) -> dict[str, Any]:
        envelope = self._decode_envelope(response)
        self._check_schema_or_raise(envelope)
        if response.status_code >= 400 or envelope.get("ok") is False:
            raise DaemonAPIError(envelope, status_code=response.status_code)
        return envelope

    # -- high-level verbs --------------------------------------------

    def info(self) -> dict[str, Any]:
        """``GET /v0/daemon.info`` — useful for connectivity smoke checks."""
        return self._parse_ok_envelope(self._request("GET", "/v0/daemon.info"))

    def list_recordings(self) -> dict[str, Any]:
        return self._parse_ok_envelope(self._request("GET", "/v0/recording.list"))

    def snapshot(self) -> dict[str, Any]:
        return self._parse_ok_envelope(self._request("GET", "/v0/session.snapshot"))

    def start(self, **payload: Any) -> dict[str, Any]:
        # The daemon mirrors Phase 1's RecordingStartRequest shape; we
        # let it validate. Keyword-only args keep call sites readable.
        return self._parse_ok_envelope(
            self._request("POST", "/v0/recording.start", json_body=payload)
        )

    def stop(
        self,
        *,
        force: bool = False,
        timeout: "httpx.Timeout | float | None" = None,
        **extra: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"force": force}
        body.update(extra)
        return self._parse_ok_envelope(
            self._request("POST", "/v0/recording.stop", json_body=body, timeout=timeout)
        )

    def content_search(
        self,
        query: str,
        *,
        recording: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v0/content.search`` — on-screen-text snippets + pointers."""
        body: dict[str, Any] = {"query": query}
        if recording is not None:
            body["recording"] = recording
        if limit is not None:
            body["limit"] = limit
        return self._parse_ok_envelope(
            self._request("POST", "/v0/content.search", json_body=body)
        )

    def transcript_search(
        self,
        query: str,
        *,
        recording: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v0/transcript.search`` — keyword hits over scrubbed transcripts."""
        body: dict[str, Any] = {"query": query}
        if recording is not None:
            body["recording"] = recording
        if limit is not None:
            body["limit"] = limit
        return self._parse_ok_envelope(
            self._request("POST", "/v0/transcript.search", json_body=body)
        )

    def timeline_query(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        app: str | None = None,
        recording: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v0/timeline.query`` — structured app/window/time rows."""
        body: dict[str, Any] = {}
        for key, value in (
            ("start_ms", start_ms),
            ("end_ms", end_ms),
            ("app", app),
            ("recording", recording),
            ("limit", limit),
        ):
            if value is not None:
                body[key] = value
        return self._parse_ok_envelope(
            self._request("POST", "/v0/timeline.query", json_body=body)
        )

    def entitlement_refresh(self) -> dict[str, Any]:
        """``POST /v0/entitlement.refresh`` — force a daemon-context ID-token re-mint (U14).

        The post-checkout signal: the daemon holds the refresh token, so it
        re-mints its cached ID token and re-arms the KTD-4 entitlement lease that
        the local recording/recall gates (U8/U9) read — un-gating a just-converted
        user promptly instead of waiting out the ~1h token buffer. Returns the
        refreshed ``whoami`` envelope (``signed_in``/``tier``/``trial_end``/…).
        """
        return self._parse_ok_envelope(
            self._request("POST", "/v0/entitlement.refresh", json_body={})
        )

    def backfill_start(self) -> dict[str, Any]:
        """``POST /v0/backfill.start`` — start (or resume) the content-index backfill.

        Idempotent on the daemon side: starting while a run is in flight
        returns the existing job's status snapshot. The public verb takes
        no parameters (the daemon enumerates recordings server-side).
        """
        return self._parse_ok_envelope(
            self._request("POST", "/v0/backfill.start", json_body={})
        )

    def backfill_status(self) -> dict[str, Any]:
        """``GET /v0/backfill.status`` — privacy-safe backfill status snapshot."""
        return self._parse_ok_envelope(self._request("GET", "/v0/backfill.status"))

    def backfill_cancel(self) -> dict[str, Any]:
        """``POST /v0/backfill.cancel`` — signal the in-flight run to stop."""
        return self._parse_ok_envelope(
            self._request("POST", "/v0/backfill.cancel", json_body={})
        )

    def storage_migrate(self, target: str) -> dict[str, Any]:
        """``POST /v0/storage.migrate`` — relocate the recordings library (SCR-228).

        Synchronous same-volume move on the daemon side. On a refusal the daemon
        returns ``ok:false`` with ``error=storage_migration_failed`` plus a
        specific ``reason`` and human ``message`` (carried on the raised
        ``DaemonClientError.envelope``).
        """
        return self._parse_ok_envelope(
            self._request(
                "POST", "/v0/storage.migrate", json_body={"target": target}
            )
        )

    def model_download_start(self, model_id: str | None = None) -> dict[str, Any]:
        """``POST /v0/model.download.start`` — start (or return) the model download."""
        body = {"model_id": model_id} if model_id else {}
        return self._parse_ok_envelope(
            self._request("POST", "/v0/model.download.start", json_body=body)
        )

    def model_download_status(self) -> dict[str, Any]:
        """``GET /v0/model.download.status`` — current download snapshot."""
        return self._parse_ok_envelope(
            self._request("GET", "/v0/model.download.status")
        )

    def model_download_cancel(self) -> dict[str, Any]:
        """``POST /v0/model.download.cancel`` — signal the download to stop."""
        return self._parse_ok_envelope(
            self._request("POST", "/v0/model.download.cancel", json_body={})
        )

    def model_status(self) -> dict[str, Any]:
        """``GET /v0/model.status`` — installed-model snapshot."""
        return self._parse_ok_envelope(self._request("GET", "/v0/model.status"))

    @contextmanager
    def events(
        self,
        *,
        since: int | None = None,
        read_timeout: float | None = None,
    ) -> Iterator[Iterator[dict[str, Any]]]:
        """NDJSON event stream context manager.

        Yields an iterator of decoded JSON event dicts. Caller iterates
        until termination (``recording_finalized``, ``_close`` frame, or
        upstream EOF). The context manager closes the underlying
        streaming response on exit so the daemon-side subscription is
        torn down promptly.

        ``since`` is the bus cursor the caller wants to resume from;
        omit for "from now". A cursor older than the retained replay
        window surfaces as a ``DaemonAPIError`` with the daemon's
        ``cursor_unknown`` envelope.
        """
        params = {"since": since} if since is not None else None
        response = self._request(
            "GET",
            "/v0/events",
            params=params,
            stream=True,
            # Use a longer read timeout for events; the call site may
            # override per-poll. ``None`` disables the read deadline.
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=read_timeout if read_timeout is not None else None,
                write=self._connect_timeout,
                pool=self._connect_timeout,
            ),
        )
        try:
            if response.status_code >= 400:
                # Must read the body before decoding JSON — httpx raises
                # ResponseNotRead if you call response.json() on a
                # streaming response that has not been consumed yet.
                response.read()
                envelope = self._decode_envelope(response)
                self._check_schema_or_raise(envelope)
                raise DaemonAPIError(envelope, status_code=response.status_code)

            def _iter() -> Iterator[dict[str, Any]]:
                for raw_line in response.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        # Skip malformed lines — the daemon owns the
                        # contract; a bad line is a daemon bug, not a
                        # client one. Surface via the daemon stderr log.
                        continue
                    if isinstance(event, dict):
                        yield event

            yield _iter()
        finally:
            response.close()
