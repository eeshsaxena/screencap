"""Unit tests for the daemon-observable verdict inputs (U1, honest status).

Covers the assembly of ``intelligence_verdict_inputs`` from the config getters +
install marker, and the ``_macos_floor_ok`` helper across the Darwin/version matrix.
The individual getters have their own tests; here we pin that U1 wires each fact
into the right key and never probes Apple-Intelligence availability.
"""

from __future__ import annotations

import screencap.config as config
import screencap.models.download as download_mod
import screencap.segmentation.availability as availability
from screencap.segmentation.availability import (
    _macos_floor_ok,
    intelligence_verdict_inputs,
)


def _stub(
    monkeypatch,
    *,
    provider="on-device",
    cloud=None,
    consent=True,
    installed=True,
    floor=True,
) -> None:
    monkeypatch.setattr(config, "get_llm_provider", lambda: provider)
    monkeypatch.setattr(config, "get_llm_cloud_provider", lambda: cloud)
    monkeypatch.setattr(config, "get_summary_cloud_consent", lambda: consent)
    monkeypatch.setattr(download_mod, "is_model_installed", lambda *a, **k: installed)
    monkeypatch.setattr(availability, "_macos_floor_ok", lambda: floor)


def test_on_device_with_downloaded_model(monkeypatch):
    _stub(monkeypatch, provider="on-device", cloud=None, installed=True, floor=True)
    assert intelligence_verdict_inputs() == {
        "active_provider": "on-device",
        "downloaded_model_installed": True,
        "cloud_provider": None,
        "cloud_summary_consent": True,
        "os_floor_ok": True,
    }


def test_cloud_provider_with_summary_consent(monkeypatch):
    _stub(monkeypatch, cloud="gemini", consent=True, installed=False)
    out = intelligence_verdict_inputs()
    assert out["cloud_provider"] == "gemini"
    assert out["cloud_summary_consent"] is True
    assert out["downloaded_model_installed"] is False


def test_summary_consent_can_be_disabled(monkeypatch):
    _stub(monkeypatch, consent=False, installed=False)
    assert intelligence_verdict_inputs()["cloud_summary_consent"] is False


def test_below_os_floor(monkeypatch):
    _stub(monkeypatch, floor=False)
    assert intelligence_verdict_inputs()["os_floor_ok"] is False


def test_floor_helper_darwin_at_or_above_26(monkeypatch):
    monkeypatch.setattr(availability.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(availability.platform, "mac_ver", lambda: ("26.1", ("", "", ""), ""))
    assert _macos_floor_ok() is True


def test_floor_helper_darwin_below_26(monkeypatch):
    monkeypatch.setattr(availability.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(availability.platform, "mac_ver", lambda: ("15.5", ("", "", ""), ""))
    assert _macos_floor_ok() is False


def test_floor_helper_non_darwin(monkeypatch):
    monkeypatch.setattr(availability.platform, "system", lambda: "Linux")
    assert _macos_floor_ok() is False


def test_floor_helper_empty_release(monkeypatch):
    monkeypatch.setattr(availability.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(availability.platform, "mac_ver", lambda: ("", ("", "", ""), ""))
    assert _macos_floor_ok() is False
