"""Daemon model-download job + verbs (U5, SCR-239).

Drives the ModelDownloadJob against a fake bus + injected download engine (no
real Hugging Face fetch): lifecycle (start → progress → terminal), idempotency,
cancel, engine-error → failed, the busy predicate, the progress throttle, and the
route wiring.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from screencap.daemon.model_download_job import (
    EVENT_MODEL_DOWNLOAD_COMPLETED,
    EVENT_MODEL_DOWNLOAD_FAILED,
    ModelDownloadJob,
)
from screencap.models.download import DownloadResult


class FakeBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)

    def subscriber_count(self):
        return 0

    async def shutdown(self):
        pass


def _patch_download(monkeypatch, fn):
    monkeypatch.setattr("screencap.models.download.download_model", fn)


async def _drain(job):
    if job._task is not None:
        await job._task
    for _ in range(5):
        await asyncio.sleep(0)  # flush any run_coroutine_threadsafe progress publishes


class TestLifecycle:
    async def test_start_progress_completed(self, monkeypatch):
        def fake(model_id, *, progress_cb, stop_event, **kw):
            progress_cb(0, 100)
            progress_cb(100, 100)
            return DownloadResult("installed", model_id or "m", "llamacpp")

        _patch_download(monkeypatch, fake)
        bus = FakeBus()
        job = ModelDownloadJob(bus)
        job.start("m")
        await _drain(job)

        assert job.status().state == "installed"
        types = [e["type"] for e in bus.events]
        assert EVENT_MODEL_DOWNLOAD_COMPLETED in types
        # No recording context leaks into any payload.
        for e in bus.events:
            assert "recording" not in e and "path" not in e and "url" not in e

    async def test_engine_error_reports_failed(self, monkeypatch):
        def boom(model_id, *, progress_cb, stop_event, **kw):
            raise RuntimeError("network down")

        _patch_download(monkeypatch, boom)
        bus = FakeBus()
        job = ModelDownloadJob(bus)
        job.start("m")
        await _drain(job)

        assert job.status().state == "failed"
        assert EVENT_MODEL_DOWNLOAD_FAILED in [e["type"] for e in bus.events]
        assert job.is_running() is False  # daemon stays up

    async def test_unpinned_model_reports_not_release_pinned(self, monkeypatch):
        # The shipped default model is PLACEHOLDER-pinned, so download_model raises
        # ModelNotPinnedError — the job must surface a distinct, greppable reason
        # rather than the opaque "job-crashed" of the broad-Exception handler.
        from screencap.models.download import ModelNotPinnedError

        def unpinned(model_id, *, progress_cb, stop_event, **kw):
            raise ModelNotPinnedError("qwen2.5-3b-instruct is not release-pinned")

        _patch_download(monkeypatch, unpinned)
        bus = FakeBus()
        job = ModelDownloadJob(bus)
        job.start("qwen2.5-3b-instruct")
        await _drain(job)

        assert job.status().state == "failed"
        assert job.status().reason == "not-release-pinned"
        assert EVENT_MODEL_DOWNLOAD_FAILED in [e["type"] for e in bus.events]
        assert job.is_running() is False

    async def test_failed_result_maps_to_failed_event(self, monkeypatch):
        def fake(model_id, *, progress_cb, stop_event, **kw):
            return DownloadResult("failed", model_id or "m", "llamacpp",
                                  reason="sha256-mismatch:model.gguf")

        _patch_download(monkeypatch, fake)
        bus = FakeBus()
        job = ModelDownloadJob(bus)
        job.start("m")
        await _drain(job)
        assert job.status().state == "failed"
        assert job.status().reason == "sha256-mismatch:model.gguf"


class TestConcurrencyAndCancel:
    async def test_idempotent_start_and_busy_predicate(self, monkeypatch):
        gate = threading.Event()

        def blocking(model_id, *, progress_cb, stop_event, **kw):
            gate.wait(timeout=2)
            return DownloadResult("installed", model_id or "m", "llamacpp")

        _patch_download(monkeypatch, blocking)
        bus = FakeBus()
        job = ModelDownloadJob(bus)

        job.start("m")
        await asyncio.sleep(0)  # let the task get into the blocking engine call
        assert job.is_running() is True

        # Second start while in flight returns the in-flight snapshot (no 2nd task).
        first_task = job._task
        snap = job.start("m")
        assert snap.state == "downloading"
        assert job._task is first_task

        gate.set()
        await _drain(job)
        assert job.status().state == "installed"

    async def test_cancel_sets_stop_and_converges(self, monkeypatch):
        def cancellable(model_id, *, progress_cb, stop_event, **kw):
            # Engine observes the stop flag and returns cancelled.
            for _ in range(100):
                if stop_event.is_set():
                    return DownloadResult("cancelled", model_id or "m", "llamacpp")
            return DownloadResult("installed", model_id or "m", "llamacpp")

        _patch_download(monkeypatch, cancellable)
        bus = FakeBus()
        job = ModelDownloadJob(bus)
        job.start("m")
        job.cancel()
        await _drain(job)
        assert job.status().state == "cancelled"


class TestThrottle:
    def test_should_emit_throttles_tiny_increments(self):
        job = ModelDownloadJob(FakeBus())
        assert job._should_emit(0, 100) is True  # first is always emitted
        assert job._should_emit(1, 10_000) is False  # +0.01% within 0.5s → skip
        assert job._should_emit(200, 10_000) is True  # +2% → emit

    def test_status_default_is_idle(self):
        assert ModelDownloadJob(FakeBus()).status().state == "idle"


class TestRouteWiring:
    def test_app_registers_model_routes(self):
        from screencap.daemon.app import build_app

        app = build_app()
        paths = {r.path for r in app.routes}
        assert {
            "/v0/model.download.start",
            "/v0/model.download.status",
            "/v0/model.download.cancel",
            "/v0/model.status",
        } <= paths

    def test_start_request_schema_validates(self):
        from screencap.daemon import schema

        req = schema.ModelDownloadStartRequest.model_validate({"model_id": "m"})
        assert req.model_id == "m"
        assert schema.ModelDownloadStartRequest.model_validate({}).model_id is None
