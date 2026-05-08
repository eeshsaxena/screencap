"""Daemon API contract models and response envelope helpers."""

from __future__ import annotations

from functools import cache
from typing import Any

API_SCHEMA_VERSION = 1
_DAEMON_INFO_API_VERSION = 1
_LIST_API_VERSION = 1
_SNAPSHOT_API_VERSION = 1
_EVENTS_API_VERSION = 1
_RECORDING_START_API_VERSION = 1
_RECORDING_STOP_API_VERSION = 1


@cache
def daemon_version() -> str:
    """Return the installed ScreenCap package version used by this daemon."""
    from screencap import __version__

    return str(__version__)


def envelope(*, schema_version: int, ok: bool = True, **payload: Any) -> dict[str, Any]:
    """Build the uniform daemon API envelope used by every response."""
    return {
        "ok": ok,
        "schema_version": schema_version,
        "daemon_version": daemon_version(),
        "api_schema_version": API_SCHEMA_VERSION,
        **payload,
    }


_MODEL_NAMES = {
    "EnvelopeResponse",
    "DaemonInfoResponse",
    "RecordingSummary",
    "ListResponse",
    "SessionSnapshotResponse",
    "RecordingStartRequest",
    "RecordingStartResponse",
    "RecordingStopRequest",
    "RecordingStopResponse",
}
_MODELS: dict[str, Any] | None = None


def _load_models() -> dict[str, Any]:
    """Construct Pydantic models on first use, keeping helpers Pydantic-free."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS

    from pydantic import BaseModel, ConfigDict

    class _DaemonModel(BaseModel):
        """Shared Pydantic model settings for documented daemon responses."""

        model_config = ConfigDict(extra="ignore")

    class EnvelopeResponse(_DaemonModel):
        ok: bool
        schema_version: int
        daemon_version: str
        api_schema_version: int

    class DaemonInfoResponse(EnvelopeResponse):
        build: str | None
        started_at: float

    class RecordingSummary(_DaemonModel):
        """Recording summary shape returned by ``catalog.list_recordings()``."""

        name: str
        date: str
        duration: str
        size_mb: str
        has_audio: bool
        transcribed: bool
        uploaded: bool
        drops: dict[str, int] | None = None
        is_stub: bool = False
        chunks_total: int = 0
        chunks_uploaded: int = 0
        intent: str | None = None
        started_at: float | None = None
        duration_seconds: float | None = None

    class ListResponse(EnvelopeResponse):
        recordings: list[RecordingSummary]

    class SessionSnapshotResponse(EnvelopeResponse):
        is_recording: bool | None
        daemon_owned: bool
        recording_name: str | None
        started_at: float | None
        claimant: str | None
        recovering: bool = False
        claimant_pid: int | None = None
        claimant_started_at: float | None = None
        engine_pid: int | None = None
        frames_written: int | None = None
        cursor: int

    class RecordingStartRequest(_DaemonModel):
        name: str | None = None
        started_by: str | None = None
        description: str | None = None
        audio: bool | None = None
        output_dir: str | None = None
        wifi_metrics: bool | None = None
        app_versions: bool | None = None
        force_clean: bool = False
        capture_video: bool | None = None
        capture_images: bool | None = None
        capture_window_data: bool | None = None
        verbose: bool = False
        chunk_duration: float | None = None
        live_upload: bool = True
        force_mode: str | None = None
        cloud_intent: bool = False
        keep_local: bool = True
        intent_source: str = "flag"
        segmentation_mode: str = "llm"
        scrub_enabled: bool = True
        show_on_website: bool = True
        network: bool = False

    class RecordingStartResponse(EnvelopeResponse):
        session_id: str
        started_at: float

    class RecordingStopRequest(_DaemonModel):
        force: bool = False
        expected_claimant_pid: int | None = None
        expected_started_at: float | None = None

    class RecordingStopResponse(EnvelopeResponse):
        stopped: bool
        final_state: str

    _MODELS = {
        "EnvelopeResponse": EnvelopeResponse,
        "DaemonInfoResponse": DaemonInfoResponse,
        "RecordingSummary": RecordingSummary,
        "ListResponse": ListResponse,
        "SessionSnapshotResponse": SessionSnapshotResponse,
        "RecordingStartRequest": RecordingStartRequest,
        "RecordingStartResponse": RecordingStartResponse,
        "RecordingStopRequest": RecordingStopRequest,
        "RecordingStopResponse": RecordingStopResponse,
    }
    globals().update(_MODELS)
    return _MODELS


def __getattr__(name: str) -> Any:
    if name in _MODEL_NAMES:
        return _load_models()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_MODEL_NAMES])


__all__ = [
    "API_SCHEMA_VERSION",
    "_DAEMON_INFO_API_VERSION",
    "_LIST_API_VERSION",
    "_SNAPSHOT_API_VERSION",
    "_EVENTS_API_VERSION",
    "_RECORDING_START_API_VERSION",
    "_RECORDING_STOP_API_VERSION",
    "daemon_version",
    "envelope",
] + sorted(_MODEL_NAMES)
