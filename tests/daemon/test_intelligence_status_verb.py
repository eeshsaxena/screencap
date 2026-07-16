"""HTTP-handler test for the ``/v0/intelligence.status`` daemon verb (U1).

The verb reports the daemon-observable config facts the app combines with its own
fresh Apple-Intelligence availability probe. The handler ignores the request on the
happy path, so it is driven directly here (no supervisor/socket needed).
"""

from __future__ import annotations

import asyncio
import json

import screencap.config as config
import screencap.models.download as download_mod
import screencap.segmentation.availability as availability
from screencap.daemon import schema
from screencap.daemon.app import intelligence_status


def test_intelligence_status_returns_verdict_inputs(monkeypatch):
    monkeypatch.setattr(config, "get_llm_provider", lambda: "on-device")
    monkeypatch.setattr(config, "get_llm_cloud_provider", lambda: None)
    monkeypatch.setattr(config, "get_summary_cloud_consent", lambda: True)
    monkeypatch.setattr(download_mod, "is_model_installed", lambda *a, **k: False)
    monkeypatch.setattr(availability, "_macos_floor_ok", lambda: True)

    resp = asyncio.run(intelligence_status(request=None))
    body = json.loads(bytes(resp.body))

    assert body["ok"] is True
    assert body["schema_version"] == schema._MODELS_API_VERSION
    vi = body["verdict_inputs"]
    assert set(vi) == {
        "active_provider",
        "downloaded_model_installed",
        "cloud_provider",
        "cloud_summary_consent",
        "os_floor_ok",
    }
    assert vi["active_provider"] == "on-device"
    assert vi["downloaded_model_installed"] is False
    assert vi["cloud_provider"] is None
