"""Async HTTP-over-AF_UNIX client used by the ``screencap mcp`` server.

The synchronous ``cli._daemon_client.DaemonHTTPClient`` cannot be reused inside
async FastMCP tools, so the MCP server owns its own ``httpx.AsyncClient`` over an
``AsyncHTTPTransport(uds=...)``. This module has NO ``mcp`` dependency so the
forwarding/envelope logic is testable against a test daemon (ASGITransport)
without the SDK.

It also owns the held ``/v0/events`` liveness subscription: the daemon's
idle-shutdown deliberately excludes read verbs from its activity set (so
cron-polling can't pin an auto-spawned daemon open), so the MCP server keeps the
daemon alive for the session by holding one event subscription open — the
existing ``subscriber_count() > 0`` busy path — which self-clears on disconnect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx


class DaemonError(RuntimeError):
    """A daemon verb returned an error envelope or was unreachable."""


class AsyncDaemonClient:
    """Async client for the daemon's read verbs over the UNIX socket."""

    def __init__(
        self,
        socket_path: Path | str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout: float = 2.0,
        read_timeout: float = 30.0,
    ) -> None:
        if transport is None:
            if socket_path is None:
                from screencap.daemon.socket import default_socket_path

                socket_path = default_socket_path()
            transport = httpx.AsyncHTTPTransport(uds=str(Path(socket_path)))
        self._client = httpx.AsyncClient(
            base_url="http://screencap",
            transport=transport,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=connect_timeout,
            ),
            trust_env=False,
        )

    @property
    def raw(self) -> httpx.AsyncClient:
        """The underlying client (used by the held liveness subscription)."""
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- envelope helpers ------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise DaemonError(f"daemon unreachable at {path}: {exc}") from exc
        return self._ok(path, resp)

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise DaemonError(f"daemon unreachable at {path}: {exc}") from exc
        return self._ok(path, resp)

    @staticmethod
    def _ok(path: str, resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise DaemonError(f"daemon returned non-JSON for {path}") from exc
        if resp.status_code >= 400 or not (isinstance(data, dict) and data.get("ok")):
            code = data.get("error") if isinstance(data, dict) else None
            raise DaemonError(f"daemon error for {path}: {code or resp.status_code}")
        return data

    # -- read verbs ------------------------------------------------------

    async def list_recordings(self) -> dict[str, Any]:
        return await self._get("/v0/recording.list")

    async def whoami(self) -> dict[str, Any]:
        return await self._get("/v0/auth.whoami")

    async def content_search(
        self, query: str, *, recording: str | None = None, limit: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if recording is not None:
            body["recording"] = recording
        if limit is not None:
            body["limit"] = limit
        return await self._post("/v0/content.search", body)

    async def transcript_search(
        self, query: str, *, recording: str | None = None, limit: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if recording is not None:
            body["recording"] = recording
        if limit is not None:
            body["limit"] = limit
        return await self._post("/v0/transcript.search", body)

    async def timeline_query(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        app: str | None = None,
        recording: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
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
        return await self._post("/v0/timeline.query", body)

    async def frame_nearest(
        self,
        recording: str,
        timestamp_ms: int,
        *,
        staleness_cap_ms: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"recording": recording, "timestamp_ms": timestamp_ms}
        if staleness_cap_ms is not None:
            body["staleness_cap_ms"] = staleness_cap_ms
        return await self._post("/v0/frame.nearest", body)


class LivenessSubscription:
    """Holds one ``/v0/events`` subscription open for the agent session.

    Opening-and-discarding (or letting a read-timeout close it) would trip the
    daemon's ``finally: bus.remove(sub)`` and drop ``subscriber_count()`` to 0,
    idle-reaping the daemon mid-conversation. So a background task drains and
    discards frames for the whole session; the stream closes only on MCP process
    exit (or :meth:`aclose`), which self-clears the subscriber.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._resp: httpx.Response | None = None
        self._task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        req = self._client.build_request(
            "GET",
            "/v0/events",
            # read=None: the stream is long-lived and may be silent for long
            # stretches; a read deadline would close it and drop the subscriber.
            timeout=httpx.Timeout(connect=2.0, read=None, write=2.0, pool=2.0),
        )
        self._resp = await self._client.send(req, stream=True)
        lines = self._resp.aiter_lines()
        # The daemon emits a `subscribed` frame first; consume it so we know the
        # subscription is live before returning.
        await asyncio.wait_for(anext(lines), timeout=5.0)
        self._task = asyncio.create_task(self._drain(lines))

    @staticmethod
    async def _drain(lines: AsyncIterator[str]) -> None:
        try:
            async for _line in lines:
                pass  # discard — we only hold the subscription, not its data
        except asyncio.CancelledError:
            # Cooperative cancellation (aclose) — re-raise so the task ends
            # cancelled rather than completing normally.
            raise
        except Exception:
            # Any other error (HTTP, transport, decode) must stay swallowed: an
            # unhandled exception escaping this background task would surface as a
            # task-exception warning AND silently drop the /v0/events liveness
            # subscription. Holding the subscription is best-effort; on failure
            # the daemon just idle-shuts-down sooner.
            pass

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._resp is not None:
            try:
                await self._resp.aclose()
            except Exception:
                pass
            self._resp = None
