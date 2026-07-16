"""Tests for the per-task cloud-consent policy (U6).

Covers the full task x config matrix and the fixed privacy guards:

- Day-split/label is on-device-only regardless of cloud config (R7); it
  degrades to the idle-gap heuristic, never cloud (KTD6, AE2).
- Summary/title prefers on-device; resolves to cloud only when on-device is
  unavailable AND its consent row is on AND a cloud provider is configured
  (R8, the resolved decision).
- Frames/images are never-cloud for every configuration (R9, AE3).
- Recall-answer runs on-device whenever available; it is cloud-eligible as the
  fallback when on-device is unavailable and a cloud provider is configured (R10).
- The config consent getters resolve env > toml > default. The default is now
  **on**: connecting a cloud provider is the consent (KTD1), so summaries/answers
  use it automatically as the on-device-unavailable fallback. The per-task rows
  remain as an env/toml override (e.g. ``SCREENCAP_SUMMARY_CLOUD_CONSENT=0``).

These tests are self-contained; the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from screencap.segmentation.consent import (
    ConsentPolicy,
    ExecutionTarget,
    TaskKind,
    frames_may_attach,
)

# Consent is the privacy matrix — run in CI's privacy lane (Vision-free).
pytestmark = pytest.mark.privacy

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
            # SCR-272: even with the frames opt-in ON, resolve() must not weaken —
            # frames are never a standalone cloud task (that gate is separate).
            ConsentPolicy(
                cloud_provider="gemini",
                summary_cloud_consent=True,
                recall_cloud_consent=True,
                frames_cloud_consent=True,
            ),
        ]
        for policy in configs:
            for on_device in (True, False):
                assert (
                    policy.resolve(TaskKind.FRAMES, on_device_available=on_device)
                    is ExecutionTarget.NEVER
                )


# ---------------------------------------------------------------------------
# frames_may_attach — the SEPARATE frame-egress gate, layered on an already
# cloud-resolved task (SCR-272). Never touches resolve()/the FRAMES→NEVER guard.
# ---------------------------------------------------------------------------

class TestFramesMayAttach:
    def test_true_only_with_cloud_and_consent_and_provider(self):
        policy = ConsentPolicy(cloud_provider="gemini", frames_cloud_consent=True)
        assert frames_may_attach(policy, ExecutionTarget.CLOUD) is True

    def test_false_when_host_task_not_cloud(self):
        """A non-cloud host task never attaches frames, even fully consented."""
        policy = ConsentPolicy(cloud_provider="gemini", frames_cloud_consent=True)
        for target in (
            ExecutionTarget.ON_DEVICE,
            ExecutionTarget.NEVER,
            ExecutionTarget.HEURISTIC,
            ExecutionTarget.NONE,
        ):
            assert frames_may_attach(policy, target) is False

    def test_ae1_false_without_frames_consent_even_with_provider(self):
        """AE1 (privacy): connecting a cloud provider + cloud-tasks-default-on does
        NOT enable frames. With ``frames_cloud_consent`` off, no attach — even with
        a provider set and the summary/recall rows consented."""
        policy = ConsentPolicy(
            cloud_provider="gemini",
            summary_cloud_consent=True,
            recall_cloud_consent=True,
            frames_cloud_consent=False,
        )
        assert frames_may_attach(policy, ExecutionTarget.CLOUD) is False

    def test_false_when_consent_on_but_no_provider(self):
        policy = ConsentPolicy(cloud_provider=None, frames_cloud_consent=True)
        assert frames_may_attach(policy, ExecutionTarget.CLOUD) is False

    def test_default_policy_never_attaches(self):
        assert frames_may_attach(ConsentPolicy(), ExecutionTarget.CLOUD) is False


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
                "SCREENCAP_FRAMES_CLOUD_CONSENT",
            )
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {
                "intelligence": {
                    "cloud_provider": "gemini",
                    "summary_cloud_consent": True,
                    "recall_cloud_consent": False,
                    "frames_cloud_consent": True,
                }
            }
            policy = ConsentPolicy.from_config()
        assert policy.cloud_provider == "gemini"
        assert policy.summary_cloud_consent is True
        assert policy.recall_cloud_consent is False
        # SCR-272: frames opt-in wires through from_config too.
        assert policy.frames_cloud_consent is True
        assert frames_may_attach(policy, ExecutionTarget.CLOUD) is True
        # And it wires straight through to the resolver.
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=False)
            is ExecutionTarget.CLOUD
        )

    def test_from_config_default_consent_on_no_provider(self):
        """Default config: consent on by default, but with no cloud provider
        configured there is nothing to fall back to (KTD1)."""
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
        assert policy.summary_cloud_consent is True
        assert policy.recall_cloud_consent is True
        # SCR-272: frames stay OFF by default even when summary/recall default on.
        assert policy.frames_cloud_consent is False
        # Consent on, but no provider → still NONE (nothing to fall back to).
        assert (
            policy.resolve(TaskKind.SUMMARY, on_device_available=False)
            is ExecutionTarget.NONE
        )

    def test_from_config_default_reaches_cloud_when_provider_configured(self):
        """Point 1/2 (KTD1): with the shipped default and only a cloud provider
        configured (no explicit per-task consent), summaries/answers resolve to
        the cloud fallback when on-device is unavailable."""
        import screencap.config as cfg

        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("SCREENCAP_")
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg._config_cache = {"intelligence": {"cloud_provider": "gemini"}}
            policy = ConsentPolicy.from_config()
        assert policy.cloud_provider == "gemini"
        for kind in (TaskKind.SUMMARY, TaskKind.RECALL_ANSWER):
            assert (
                policy.resolve(kind, on_device_available=True)
                is ExecutionTarget.ON_DEVICE  # on-device still preferred
            )
            assert (
                policy.resolve(kind, on_device_available=False)
                is ExecutionTarget.CLOUD  # fallback fires with no toggle set
            )
        # Fixed guards hold regardless of the default flip.
        assert (
            policy.resolve(TaskKind.DAY_SPLIT, on_device_available=False)
            is ExecutionTarget.HEURISTIC
        )
        assert (
            policy.resolve(TaskKind.FRAMES, on_device_available=False)
            is ExecutionTarget.NEVER
        )


# ---------------------------------------------------------------------------
# Config consent getters — env > toml > default (summary/recall default on, KTD1)
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

    def test_summary_consent_default_on(self):
        """KTD1: default is now on — connecting a cloud provider is the consent."""
        import screencap.config as cfg
        from screencap.config import get_summary_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {}
            assert get_summary_cloud_consent() is True

    def test_summary_consent_toml_override_off(self):
        """The per-task row survives as a toml override that can disable cloud."""
        import screencap.config as cfg
        from screencap.config import get_summary_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {"intelligence": {"summary_cloud_consent": False}}
            assert get_summary_cloud_consent() is False

    def test_summary_consent_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_summary_cloud_consent

        # Env override wins both ways: off over a toml on, and on over a toml off.
        with mock.patch.dict(
            os.environ, {"SCREENCAP_SUMMARY_CLOUD_CONSENT": "0"}
        ):
            cfg._config_cache = {"intelligence": {"summary_cloud_consent": True}}
            assert get_summary_cloud_consent() is False
        with mock.patch.dict(
            os.environ, {"SCREENCAP_SUMMARY_CLOUD_CONSENT": "1"}
        ):
            cfg._config_cache = {"intelligence": {"summary_cloud_consent": False}}
            assert get_summary_cloud_consent() is True

    def test_recall_consent_default_on(self):
        """KTD1: default is now on — connecting a cloud provider is the consent."""
        import screencap.config as cfg
        from screencap.config import get_recall_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {}
            assert get_recall_cloud_consent() is True

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

    def test_frames_consent_default_off(self):
        """SCR-272: unlike summary/recall, frames default OFF — connecting a cloud
        provider + cloud-tasks-on does NOT enable frame egress."""
        import screencap.config as cfg
        from screencap.config import get_frames_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {}
            assert get_frames_cloud_consent() is False

    def test_frames_consent_toml_override_on(self):
        import screencap.config as cfg
        from screencap.config import get_frames_cloud_consent

        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            cfg._config_cache = {"intelligence": {"frames_cloud_consent": True}}
            assert get_frames_cloud_consent() is True

    def test_frames_consent_env_overrides_toml(self):
        import screencap.config as cfg
        from screencap.config import get_frames_cloud_consent

        # Env wins both ways over toml.
        with mock.patch.dict(
            os.environ, {"SCREENCAP_FRAMES_CLOUD_CONSENT": "1"}
        ):
            cfg._config_cache = {"intelligence": {"frames_cloud_consent": False}}
            assert get_frames_cloud_consent() is True
        with mock.patch.dict(
            os.environ, {"SCREENCAP_FRAMES_CLOUD_CONSENT": "0"}
        ):
            cfg._config_cache = {"intelligence": {"frames_cloud_consent": True}}
            assert get_frames_cloud_consent() is False
