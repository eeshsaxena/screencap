#!/usr/bin/env python3
"""MCP-shaped smoke check for daemon recording verbs and event stream."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import AsyncIterator

import httpx


def _default_socket_path() -> Path:
    return Path.home() / ".screencap" / "run" / "api.sock"


def _print(status: str, message: str) -> None:
    print(f"{status}: {message}")


async def _read_json_line(lines: AsyncIterator[str], timeout: float = 10.0) -> dict:
    line = await asyncio.wait_for(anext(lines), timeout=timeout)
    return json.loads(line)


async def _read_until(lines: AsyncIterator[str], event_type: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for event {event_type!r}")
        event = await _read_json_line(lines, timeout=remaining)
        if event.get("type") == event_type:
            return event


async def _run() -> int:
    socket_path = _default_socket_path()
    if not socket_path.exists():
        _print("SKIPPED", f"daemon socket not found at {socket_path}")
        return 0

    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://screencap") as client:
        try:
            info = await client.get("/v0/daemon.info", timeout=3.0)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            _print("SKIPPED", f"daemon unavailable: {exc}")
            return 0
        if info.status_code != 200 or not info.json().get("ok"):
            _print("FAIL", f"daemon.info unexpected response: {info.status_code} {info.text}")
            return 1

        snapshot = await client.get("/v0/session.snapshot", timeout=3.0)
        if snapshot.status_code != 200 or not snapshot.json().get("ok"):
            _print("FAIL", f"session.snapshot unexpected response: {snapshot.status_code} {snapshot.text}")
            return 1
        cursor = snapshot.json()["cursor"]

        async with client.stream("GET", f"/v0/events?since={cursor}", timeout=None) as stream:
            lines = stream.aiter_lines()
            subscribed = await _read_json_line(lines, timeout=5.0)
            if subscribed.get("type") != "subscribed":
                _print("FAIL", f"expected subscribed frame, got {subscribed}")
                return 1

            name = f"mcp-smoke-{int(time.time())}"
            start = await client.post(
                "/v0/recording.start",
                json={"name": name, "started_by": "mcp-contract-smoke"},
                timeout=15.0,
            )
            if start.status_code != 200 or not start.json().get("ok"):
                payload = start.json() if start.headers.get("content-type", "").startswith("application/json") else {}
                if payload.get("error") in {"permission_lost", "reconciling"}:
                    _print("SKIPPED", f"recording.start not runnable in this environment: {payload}")
                    return 0
                _print("FAIL", f"recording.start unexpected response: {start.status_code} {start.text}")
                return 1

            started = await _read_until(lines, "started", timeout=10.0)
            mid = await client.get("/v0/session.snapshot", timeout=3.0)
            mid_payload = mid.json()
            if not mid_payload.get("is_recording") or not mid_payload.get("daemon_owned"):
                _print("FAIL", f"mid-recording snapshot lacks daemon-owned state: {mid_payload}")
                return 1

            stop = await client.post("/v0/recording.stop", json={}, timeout=35.0)
            if stop.status_code != 200 or not stop.json().get("ok"):
                _print("FAIL", f"recording.stop unexpected response: {stop.status_code} {stop.text}")
                return 1
            finalized = await _read_until(lines, "recording_finalized", timeout=35.0)

    _print(
        "PASS",
        "daemon verbs support agent flow: "
        f"cursor={cursor}, started_cursor={started.get('cursor')}, "
        f"finalized_cursor={finalized.get('cursor')}",
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except TimeoutError as exc:
        _print("FAIL", str(exc))
        return 1
    except KeyboardInterrupt:
        _print("FAIL", "interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
