"""Tests for the per-task cloud-consent policy (U6).

Covers the full task x config matrix and the fixed privacy guards:

- Day-split/label is on-device-only regardless of cloud config (R7); it
  degrades to the idle-gap heuristic, never cloud (KTD6, AE2).
- Summary/title prefers on-device; resolves to cloud only when on-device is
  unavailable AND its consent row is on AND a cloud provider is configured
  (R8, the resolved decision).
- Frames/images are never-cloud for every configuration (R9, AE3).
- Recall-answer runs on-device by default and is cloud-eligible only when its
  opt-in row is added (R10).
- The config consent getters resolve env > toml > default (default off).

These tests are self-contained; the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import os
from unittest import mock

from screencap.segmentation.consent import (
    ConsentPolicy,
    ExecutionTarget,
    TaskKind,
)

# ---------------------------------------------------------------------------
# Day-split / label — on-device only, never cloud (R7 / KTD6, AE2)
# ---------------------------------------------------------------------------

class TestDaySplitOnDeviceOnly:
    def test_on_device_when_available(self):
        policy = ConsentPolicy()
        assert (
            policy.resolve(TaskKind.DAY_SPLIT, on_device_available=True)
            is ExecutionTarget.ON_DEVICE
        )

    def test_heuristic_when_unavailable(self):
        policy = ConsentPolicy()
        assert (
            policy.resolve(TaskKind.DAY_SPLIT, on_device_available=False)
            is ExecutionTarget.HEURISTIC
        )

    def test_never_cloud_even_with_cloud_key_and_summary_consent(self):
        """Covers AE2: cloud config cannot pull day-split off-device."""
        policy = ConsentPolicy(
            cloud_provider="gemini",
            summary_cloud_consent=True,
            recall_cloud_consent=True,
        )
        # Available → on-device.
        assert (
            policy.resolve(TaskKind.DAY_SPLIT, on_device_available=True)
            is ExecutionTarget.ON_DEVICE
        )
        # Unavailable → heuristic, still never cloud.
        assert (
            policy.resolve(TaskKind.DAY_SPLIT, on_device_available=False)
            is ExecutionTarget.HEURISTIC
        )


# ---------------------------------------------------------------------------
# Summary / title — prefer on-device, cloud is the consented fallback (R8)
# ---------------------------------------------------------------------------

class TestSummaryPrefersOnDevice:
    def test_on_device_when_available_even_with_consent(self):
        policy = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=True)
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=True)
            is ExecutionTarget.ON_DEVICE
        )

    def test_cloud_only_when_unavailable_and_consent_and_provider(self):
        policy = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=True)
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=False)
            is ExecutionTarget.CLOUD
        )

    def test_none_when_unavailable_and_consent_but_no_provider(self):
        """Consent on but no cloud backend configured → nothing to fall back to."""
        policy = ConsentPolicy(cloud_provider=None, summary_cloud_consent=True)
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=False)
            is ExecutionTarget.NONE
        )

    def test_none_when_unavailable_and_no_consent(self):
        policy = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=False)
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=False)
            is ExecutionTarget.NONE
        )

    def test_on_device_when_available_no_consent(self):
        policy = ConsentPolicy()
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=True)
            is ExecutionTarget.ON_DEVICE
        )


# ---------------------------------------------------------------------------
# Frames / images — never cloud for every configuration (R9, AE3)
# ---------------------------------------------------------------------------

class TestFramesNeverCloud:
    def test_never_for_every_configuration(self):
        configs = [
            ConsentPolicy(),
            ConsentPolicy(cloud_provider="gemini"),
            ConsentPolicy(
                cloud_provider="gemini",
                summary_cloud_consent=True,
                recall_cloud_consent=True,
            ),
        ]
        for policy in configs:
            for on_device in (True, False):
                assert (
                    policy.resolve(TaskKind.FRAMES, on_device_available=on_device)
                    is ExecutionTarget.NEVER
                )


# ---------------------------------------------------------------------------
# Recall-answer — on-device by default; cloud-eligible only via opt-in row (R10)
# ---------------------------------------------------------------------------

class TestRecallAnswer:
    def test_on_device_by_default(self):
        policy = ConsentPolicy()
        assert (
            policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=True)
            is ExecutionTarget.ON_DEVICE
        )

    def test_none_when_unavailable_and_no_opt_in(self):
        """Default (no recall consent) → no cloud fallback."""
        policy = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=True)
        assert (
            policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=False)
            is ExecutionTarget.NONE
        )

    def test_cloud_only_when_opt_in_added(self):
        policy = ConsentPolicy(cloud_provider="gemini", recall_cloud_consent=True)
        # Prefer on-device when available.
        assert (
            policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=True)
            is ExecutionTarget.ON_DEVICE
        )
        # Cloud only as the consented fallback when unavailable.
        assert (
            policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=False)
            is ExecutionTarget.CLOUD
        )

    def test_no_cloud_when_opt_in_but_no_provider(self):
        policy = ConsentPolicy(cloud_provider=None, recall_cloud_consent=True)
        assert (
            policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=False)
            is ExecutionTarget.NONE
        )


# ---------------------------------------------------------------------------
# ConsentPolicy.from_config — wiring to the config getters
# ---------------------------------------------------------------------------

class TestFromConfig:
    def test_from_config_reads_getters(self):
        import screencap.config as cfg

        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "SCREENCAP_LLM_CLOUD_PROVIDER",
                "SCREENCAP_SUMMARY_CLOUD_CONSENT",
                "SCREENCAP_RECALL_CLOUD_CONSENT",
            )
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {
                "intelligence": {
                    "cloud_provider": "gemini",
                    "summary_cloud_consent": True,
                    "recall_cloud_consent": False,
                }
            }
            policy = ConsentPolicy.from_config()
        assert policy.cloud_provider == "gemini"
        assert policy.summary_cloud_consent is True
        assert policy.recall_cloud_consent is False
        # And it wires straight through to the resolver.
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=False)
            is ExecutionTarget.CLOUD
        )

    def test_from_config_default_all_off(self):
        import screencap.config as cfg

        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("SCREENCAP_")
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {}
            policy = ConsentPolicy.from_config()
        assert policy.cloud_provider is None
        assert policy.summary_cloud_consent is False
        assert policy.recall_cloud_consent is False


# ---------------------------------------------------------------------------
# Config consent getters — env > toml > default (default off)
# ---------------------------------------------------------------------------

class TestConsentGetters:
    def _clean_env(self):
        return {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("SCREENCAP_")
        }

    def test_cloud_provider_default_none(self):
        import screencap.config as cfg
        from screencap.config import get_llm_cloud_provider

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {}
            assert get_llm_cloud_provider() is None

    def test_cloud_provider_toml(self):
        import screencap.config as cfg
        from screencap.config import get_llm_cloud_provider

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {"intelligence": {"cloud_provider": "gemini"}}
            assert get_llm_cloud_provider() == "gemini"

    def test_cloud_provider_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_llm_cloud_provider

        with mock.patch.dict(
            os.environ, {"SCREENCAP_LLM_CLOUD_PROVIDER": "  gemini  "}
        ):
            cfg._config_cache = {"intelligence": {"cloud_provider": "openai"}}
            assert get_llm_cloud_provider() == "gemini"

    def test_cloud_provider_empty_env_is_none(self):
        import screencap.config as cfg
        from screencap.config import get_llm_cloud_provider

        with mock.patch.dict(os.environ, {"SCREENCAP_LLM_CLOUD_PROVIDER": "   "}):
            cfg._config_cache = {"intelligence": {"cloud_provider": "gemini"}}
            assert get_llm_cloud_provider() is None

    def test_summary_consent_default_off(self):
        import screencap.config as cfg
        from screencap.config import get_summary_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {}
            assert get_summary_cloud_consent() is False

    def test_summary_consent_toml(self):
        import screencap.config as cfg
        from screencap.config import get_summary_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {"intelligence": {"summary_cloud_consent": True}}
            assert get_summary_cloud_consent() is True

    def test_summary_consent_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_summary_cloud_consent

        with mock.patch.dict(
            os.environ, {"SCREENCAP_SUMMARY_CLOUD_CONSENT": "1"}
        ):
            cfg._config_cache = {"intelligence": {"summary_cloud_consent": False}}
            assert get_summary_cloud_consent() is True

    def test_recall_consent_default_off(self):
        import screencap.config as cfg
        from screencap.config import get_recall_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {}
            assert get_recall_cloud_consent() is False

    def test_recall_consent_toml_and_env(self):
        import screencap.config as cfg
        from screencap.config import get_recall_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {"intelligence": {"recall_cloud_consent": True}}
            assert get_recall_cloud_consent() is True

        with mock.patch.dict(
            os.environ, {"SCREENCAP_RECALL_CLOUD_CONSENT": "false"}
        ):
            cfg._config_cache = {"intelligence": {"recall_cloud_consent": True}}
            assert get_recall_cloud_consent() is False
