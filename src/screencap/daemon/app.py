"""Minimal ASGI app for the ScreenCap daemon."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from screencap.daemon import errors, schema

_STARTED_AT = time.time()
_CURSOR = 0


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    yield


def _build_string() -> str | None:
    return os.environ.get("SCREENCAP_BUILD")


def _next_snapshot_cursor() -> int:
    global _CURSOR
    _CURSOR += 1
    # U3 advances this placeholder on snapshot reads; U4 replaces it with
    # the event bus sequence cursor.
    return _CURSOR


async def daemon_info(_request: Request) -> JSONResponse:
    return JSONResponse(
        schema.envelope(
            schema_version=schema._DAEMON_INFO_API_VERSION,
            build=_build_string(),
            started_at=_STARTED_AT,
        )
    )


async def recording_list(_request: Request) -> JSONResponse:
    from screencap import catalog

    try:
        recordings = catalog.list_recordings()
    except Exception as exc:
        return JSONResponse(
            errors.catalog_unreadable_envelope(
                reason=str(exc) or exc.__class__.__name__,
                schema_version=schema._LIST_API_VERSION,
            ),
            status_code=500,
        )

    summaries = []
    for recording in recordings:
        data = recording._asdict()
        model = schema.RecordingSummary
        expected_fields = set(model.model_fields)
        actual_fields = set(data)
        if actual_fields != expected_fields:
            missing = sorted(expected_fields - actual_fields)
            extra = sorted(actual_fields - expected_fields)
            raise RuntimeError(
                "RecordingInfo and RecordingSummary fields diverged: "
                f"missing={missing}, extra={extra}"
            )
        summaries.append(model(**data).model_dump(mode="json"))

    return JSONResponse(
        schema.envelope(
            schema_version=schema._LIST_API_VERSION,
            recordings=summaries,
        )
    )


def _empty_snapshot(*, is_recording: bool | None, cursor: int) -> dict:
    return schema.envelope(
        schema_version=schema._SNAPSHOT_API_VERSION,
        is_recording=is_recording,
        daemon_owned=False,
        recording_name=None,
        started_at=None,
        claimant=None,
        cursor=cursor,
    )


async def session_snapshot(_request: Request) -> JSONResponse:
    from screencap import pidfile

    cursor = _next_snapshot_cursor()
    active = pidfile.lock_is_active()
    metadata = pidfile.read_lock_metadata()

    if active and metadata is None:
        await asyncio.sleep(0.05)
        active = pidfile.lock_is_active()
        metadata = pidfile.read_lock_metadata()

    if active and metadata is None:
        return JSONResponse(_empty_snapshot(is_recording=None, cursor=cursor))

    if not active or metadata is None:
        return JSONResponse(_empty_snapshot(is_recording=False, cursor=cursor))

    claimant = metadata.get("claimant")
    daemon_owned = claimant == "daemon"
    recording_started_at = metadata.get("recording_started_at")
    if recording_started_at is None:
        recording_started_at = metadata.get("started_at")

    return JSONResponse(
        schema.envelope(
            schema_version=schema._SNAPSHOT_API_VERSION,
            is_recording=True,
            daemon_owned=daemon_owned,
            recording_name=metadata.get("recording_name"),
            started_at=recording_started_at,
            claimant=claimant if daemon_owned else None,
            cursor=cursor,
        )
    )


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/v0/daemon.info", daemon_info, methods=["GET"]),
            Route("/v0/recording.list", recording_list, methods=["GET"]),
            Route("/v0/session.snapshot", session_snapshot, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
