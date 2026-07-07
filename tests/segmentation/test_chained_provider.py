"""Chained on-device resolution + day-split routing + single-flight (U8, SCR-239).

Covers the AFM → downloaded cascade (only on PROVIDER_UNAVAILABLE), the provider
router that keeps REMOTE/cloud out of day-split (R5), and the process-wide
inference single-flight. Covers AE1, AE2, AE4.
"""

from __future__ import annotations

import threading

import pytest

from screencap.segmentation.inference_guard import inference_slot
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE
from screencap.segmentation.providers.chained import (
    ChainedOnDeviceProvider,
    UnavailableProvider,
)


class _Fake:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def segment(self, activity_summary):
        self.calls += 1
        return self.result


_TASKS = {"tasks": [{"name": "x"}]}


class TestChainCascade:
    def test_first_available_wins(self):
        afm, dl = _Fake(_TASKS), _Fake(PROVIDER_UNAVAILABLE)
        assert ChainedOnDeviceProvider([afm, dl]).segment({}) is _TASKS
        assert dl.calls == 0  # never reached — AFM produced tasks

    def test_cascades_on_unavailable(self):
        """Covers AE1: AFM unavailable + downloaded available → downloaded used."""
        afm, dl = _Fake(PROVIDER_UNAVAILABLE), _Fake(_TASKS)
        assert ChainedOnDeviceProvider([afm, dl]).segment({}) is _TASKS
        assert afm.calls == 1 and dl.calls == 1

    def test_none_stops_chain_no_fallthrough(self):
        """A genuine None (ran, declined) must NOT fall through to the next backend."""
        afm, dl = _Fake(None), _Fake(_TASKS)
        assert ChainedOnDeviceProvider([afm, dl]).segment({}) is None
        assert dl.calls == 0  # fail-open — downloaded is not tried

    def test_all_unavailable_returns_unavailable(self):
        """Covers AE4: both unavailable → unavailable (caller degrades to heuristic)."""
        afm, dl = _Fake(PROVIDER_UNAVAILABLE), _Fake(PROVIDER_UNAVAILABLE)
        assert ChainedOnDeviceProvider([afm, dl]).segment({}) is PROVIDER_UNAVAILABLE

    def test_empty_chain_is_unavailable(self):
        assert ChainedOnDeviceProvider([]).segment({}) is PROVIDER_UNAVAILABLE

    def test_unavailable_provider_always_unavailable(self):
        assert UnavailableProvider().segment({}) is PROVIDER_UNAVAILABLE


@pytest.mark.privacy
class TestDaySplitRouting:
    def _provider_names(self, monkeypatch, provider, endpoint=None, installed=False):
        import screencap.segmentation.routing as routing

        monkeypatch.setattr("screencap.config.get_llm_provider", lambda: provider)
        monkeypatch.setattr(
            "screencap.config.get_local_server_endpoint", lambda: endpoint
        )
        monkeypatch.setattr(routing, "_downloaded_model_installed", lambda: installed)
        return routing.build_day_split_provider()

    def test_on_device_builds_chain_without_downloaded(self, monkeypatch):
        from screencap.segmentation.providers.ondevice import OnDeviceProvider

        prov = self._provider_names(monkeypatch, "on-device", installed=False)
        assert isinstance(prov, ChainedOnDeviceProvider)
        assert [type(b) for b in prov._backends] == [OnDeviceProvider]

    def test_on_device_chain_includes_downloaded_when_installed(self, monkeypatch):
        from screencap.segmentation.providers.downloaded import DownloadedProvider

        prov = self._provider_names(monkeypatch, "on-device", installed=True)
        assert isinstance(prov, ChainedOnDeviceProvider)
        assert DownloadedProvider in [type(b) for b in prov._backends]

    def test_local_endpoint_routes_to_byo(self, monkeypatch):
        from screencap.segmentation.providers.local_server import LocalServerProvider

        prov = self._provider_names(
            monkeypatch, "local-server", endpoint="http://127.0.0.1:1234"
        )
        assert isinstance(prov, LocalServerProvider)

    def test_remote_endpoint_never_day_splits(self, monkeypatch):
        """Covers AE2: a REMOTE BYO endpoint is not eligible → UnavailableProvider."""
        prov = self._provider_names(
            monkeypatch, "local-server", endpoint="http://192.168.1.9:1234"
        )
        assert isinstance(prov, UnavailableProvider)
        assert prov.segment({}) is PROVIDER_UNAVAILABLE

    def test_cloud_provider_never_day_splits(self, monkeypatch):
        prov = self._provider_names(monkeypatch, "gemini")
        assert isinstance(prov, UnavailableProvider)


class TestSingleFlight:
    def test_second_concurrent_caller_is_skipped(self):
        with inference_slot() as first:
            assert first is True
            with inference_slot() as second:
                assert second is False  # skip-not-queue

    def test_slot_released_after_use(self):
        with inference_slot() as a:
            assert a is True
        # Released → the next caller acquires it again.
        with inference_slot() as b:
            assert b is True

    def test_slot_is_process_wide_across_threads(self):
        held = threading.Event()
        release = threading.Event()
        outcome = {}

        def worker():
            with inference_slot() as got:
                outcome["thread"] = got
                held.set()
                release.wait(timeout=2)

        t = threading.Thread(target=worker)
        t.start()
        held.wait(timeout=2)
        with inference_slot() as got_main:
            outcome["main"] = got_main  # blocked out while the thread holds it
        release.set()
        t.join(timeout=2)
        assert outcome["thread"] is True and outcome["main"] is False
