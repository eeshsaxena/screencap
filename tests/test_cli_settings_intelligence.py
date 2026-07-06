"""Tests for U8: ``screencap settings intelligence`` — the CLI Intelligence
provider + per-task cloud-consent surface.

Mirrors the ``settings privacy`` test shape (isolated config.toml, CliRunner
invoke). Verifies:
  - provider set → ``--json`` read-back round-trips;
  - cloud-consent rows persist and read back via the config getters;
  - the day-split/label and frames rows are rejected when set to cloud-on
    (clear message, non-zero exit) — R7/R9 defense in depth over U6;
  - an invalid provider value errors cleanly (non-zero exit);
  - writes land in the ``[intelligence]`` config.toml section.
"""

from __future__ import annotations

import json

import pytest
import tomllib
from click.testing import CliRunner

from screencap.cli import cli


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    import screencap.config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    # The consent getters honor env vars first — clear them so the tests read
    # what the CLI persisted, not an ambient override.
    for var in (
        "SCREENCAP_LLM_PROVIDER",
        "SCREENCAP_LLM_CLOUD_PROVIDER",
        "SCREENCAP_SUMMARY_CLOUD_CONSENT",
        "SCREENCAP_RECALL_CLOUD_CONSENT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _read_cfg() -> dict:
    from screencap.config import _CONFIG_PATH

    if not _CONFIG_PATH.exists():
        return {}
    return tomllib.loads(_CONFIG_PATH.read_text())


def _invoke(*args, as_json=False):
    runner = CliRunner()
    full_args = ["settings", "intelligence"]
    if as_json:
        full_args.append("--json")
    full_args.extend(args)
    return runner.invoke(cli, full_args, catch_exceptions=False)


def _last_json_line(text: str) -> dict:
    """Return the last JSON object line in `text` (stdout+stderr are combined)."""
    last = None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    assert last is not None, f"no JSON line found in: {text!r}"
    return last


# ---------------------------------------------------------------------------
# Provider round-trip
# ---------------------------------------------------------------------------


class TestProvider:
    def test_set_provider_then_json_read_back(self):
        w = _invoke("provider", "set", "gemini")
        assert w.exit_code == 0

        r = _invoke(as_json=True)
        payload = _last_json_line(r.output)
        assert payload["ok"] is True
        assert payload["intelligence"]["provider"] == "gemini"

    def test_set_provider_lands_where_getter_reads(self):
        # ``get_llm_provider`` reads the TOP-LEVEL ``llm_provider`` key (unlike
        # the [intelligence]-scoped cloud provider / consent rows), so the
        # write must land there for the getter to read it back.
        _invoke("provider", "set", "on-device")
        cfg = _read_cfg()
        assert cfg["llm_provider"] == "on-device"

    def test_invalid_provider_rejected(self):
        r = _invoke("provider", "set", "not-a-provider")
        assert r.exit_code != 0
        assert "must be one of" in r.output
        # Nothing persisted.
        assert "intelligence" not in _read_cfg()

    def test_provider_read_by_config_getter(self):
        _invoke("provider", "set", "gemini")
        from screencap import config

        config.invalidate_config_cache()
        assert config.get_llm_provider() == "gemini"


# ---------------------------------------------------------------------------
# Cloud provider
# ---------------------------------------------------------------------------


class TestCloudProvider:
    def test_set_cloud_provider_round_trip(self):
        _invoke("cloud_provider", "set", "gemini")
        cfg = _read_cfg()
        assert cfg["intelligence"]["cloud_provider"] == "gemini"

        from screencap import config

        config.invalidate_config_cache()
        assert config.get_llm_cloud_provider() == "gemini"

    def test_clear_cloud_provider_with_none(self):
        _invoke("cloud_provider", "set", "gemini")
        r = _invoke("cloud_provider", "set", "none")
        assert r.exit_code == 0
        cfg = _read_cfg()
        assert "cloud_provider" not in cfg.get("intelligence", {})

    def test_invalid_cloud_provider_rejected(self):
        r = _invoke("cloud_provider", "set", "claude")
        assert r.exit_code != 0
        assert "must be one of" in r.output


# ---------------------------------------------------------------------------
# Consent rows — persist + read back
# ---------------------------------------------------------------------------


class TestConsentRows:
    def test_summary_consent_persists_and_reads_back(self):
        w = _invoke("summary_cloud_consent", "set", "true")
        assert w.exit_code == 0
        assert _read_cfg()["intelligence"]["summary_cloud_consent"] is True

        from screencap import config

        config.invalidate_config_cache()
        assert config.get_summary_cloud_consent() is True

    def test_recall_consent_persists_and_reads_back(self):
        _invoke("recall_cloud_consent", "set", "true")
        from screencap import config

        config.invalidate_config_cache()
        assert config.get_recall_cloud_consent() is True

    def test_consent_false_persists(self):
        _invoke("summary_cloud_consent", "set", "true")
        _invoke("summary_cloud_consent", "set", "false")
        assert _read_cfg()["intelligence"]["summary_cloud_consent"] is False

    def test_invalid_consent_bool_rejected(self):
        r = _invoke("summary_cloud_consent", "set", "maybe")
        assert r.exit_code != 0
        assert "must be true or false" in r.output

    def test_json_read_back_reflects_consent(self):
        _invoke("summary_cloud_consent", "set", "true")
        r = _invoke(as_json=True)
        payload = _last_json_line(r.output)["intelligence"]
        assert payload["summary_cloud_consent"] is True
        assert payload["recall_cloud_consent"] is False


# ---------------------------------------------------------------------------
# Rejection edges — the fixed never-cloud rows (R7 / R9)
# ---------------------------------------------------------------------------


class TestNeverCloudRows:
    def test_day_split_row_rejected_when_set_cloud_on(self):
        r = _invoke("day_split_cloud_consent", "set", "true")
        assert r.exit_code != 0
        assert "on-device" in r.output
        # Nothing persisted.
        assert "day_split_cloud_consent" not in _read_cfg().get("intelligence", {})

    def test_frames_row_rejected_when_set_cloud_on(self):
        r = _invoke("frames_cloud_consent", "set", "true")
        assert r.exit_code != 0
        assert "never sent" in r.output
        assert "frames_cloud_consent" not in _read_cfg().get("intelligence", {})

    def test_day_split_row_rejected_even_when_set_false(self):
        """The row is not a settable knob at all — reject regardless of value,
        so it can never be persisted into the [intelligence] section."""
        r = _invoke("day_split_cloud_consent", "set", "false")
        assert r.exit_code != 0

    def test_frames_row_reported_fixed_off_in_json(self):
        r = _invoke(as_json=True)
        payload = _last_json_line(r.output)["intelligence"]
        assert payload["frames_cloud_consent"] is False
        assert payload["day_split_cloud_consent"] is False

    def test_unknown_row_rejected(self):
        r = _invoke("not_a_row", "set", "true")
        assert r.exit_code != 0
        assert "Unknown intelligence row" in r.output


# ---------------------------------------------------------------------------
# JSON envelope for writes
# ---------------------------------------------------------------------------


class TestJsonWriteEnvelope:
    def test_write_emits_ok_envelope(self):
        r = _invoke("provider", "set", "gemini", as_json=True)
        assert r.exit_code == 0
        payload = _last_json_line(r.output)
        assert payload["ok"] is True
        assert payload["row"] == "provider"
        assert payload["value"] == "gemini"
        assert payload["error"] is None

    def test_error_emits_ok_false(self):
        r = _invoke("provider", "set", "bogus", as_json=True)
        assert r.exit_code != 0
        payload = _last_json_line(r.output)
        assert payload["ok"] is False
        assert "error" in payload

    def test_never_cloud_row_emits_ok_false(self):
        r = _invoke("frames_cloud_consent", "set", "true", as_json=True)
        assert r.exit_code != 0
        payload = _last_json_line(r.output)
        assert payload["ok"] is False
        assert payload["error"].startswith("row_never_cloud")
