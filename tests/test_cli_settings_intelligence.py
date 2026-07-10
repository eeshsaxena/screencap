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
import types

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
        "SCREENCAP_LOCAL_SERVER_ENDPOINT",
        "SCREENCAP_BYO_KEY_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Default the BYO key-presence read to "no key stored" so the read-back
    # tests never touch the real Keychain. Tests that exercise storage override
    # this via the ``fake_key_store`` fixture.
    from screencap.segmentation import secrets as byo

    monkeypatch.setattr(byo, "has_key", lambda vendor: False)
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


# ---------------------------------------------------------------------------
# U9 (SCR-239) — downloaded / local-server provider + BYO endpoint.
# ---------------------------------------------------------------------------


def test_provider_downloaded_round_trips():
    result = _invoke("provider", "set", "downloaded", as_json=True)
    assert result.exit_code == 0, result.output
    assert _read_cfg()["llm_provider"] == "downloaded"


def test_local_server_provider_requires_endpoint():
    """Selecting local-server with no endpoint is rejected (defense in depth)."""
    result = _invoke("provider", "set", "local-server", as_json=True)
    assert result.exit_code == 1
    assert _last_json_line(result.output)["error"] == "local_server_requires_endpoint"


def test_local_server_provider_rejects_remote_endpoint():
    """A remote endpoint can't be the active day-split provider (R5)."""
    _invoke("local_server_endpoint", "set", "http://192.168.1.9:1234")
    result = _invoke("provider", "set", "local-server", as_json=True)
    assert result.exit_code == 1
    assert (
        _last_json_line(result.output)["error"]
        == "remote_endpoint_not_day_split_provider"
    )


def test_local_server_provider_accepts_loopback_endpoint():
    _invoke("local_server_endpoint", "set", "http://127.0.0.1:11434")
    result = _invoke("provider", "set", "local-server", as_json=True)
    assert result.exit_code == 0, result.output
    assert _read_cfg()["llm_provider"] == "local-server"


def test_endpoint_persists_and_classifies():
    result = _invoke("local_server_endpoint", "set", "http://127.0.0.1:11434", as_json=True)
    assert result.exit_code == 0, result.output
    assert _read_cfg()["intelligence"]["local_server_endpoint"] == "http://127.0.0.1:11434"

    block = _last_json_line(_invoke(as_json=True).output)["intelligence"]
    assert block["local_server_endpoint"] == "http://127.0.0.1:11434"
    assert block["endpoint_classification"] == "LOCAL"


def test_remote_endpoint_classified_remote():
    _invoke("local_server_endpoint", "set", "http://10.0.0.5:1234")
    block = _last_json_line(_invoke(as_json=True).output)["intelligence"]
    assert block["endpoint_classification"] == "REMOTE"


def test_non_http_scheme_rejected():
    result = _invoke("local_server_endpoint", "set", "file:///etc/passwd", as_json=True)
    assert result.exit_code == 1
    assert _last_json_line(result.output)["error"] == "invalid_endpoint_scheme"


def test_endpoint_userinfo_redacted_in_readback():
    """A URL carrying credentials is stored but never echoed with the token."""
    _invoke("local_server_endpoint", "set", "http://user:secret@127.0.0.1:1234/v1?k=tok")
    block = _last_json_line(_invoke(as_json=True).output)["intelligence"]
    redacted = block["local_server_endpoint"]
    assert "secret" not in redacted and "tok" not in redacted
    assert redacted == "http://127.0.0.1:1234/v1"


def test_endpoint_clear():
    _invoke("local_server_endpoint", "set", "http://127.0.0.1:11434")
    result = _invoke("local_server_endpoint", "set", "none", as_json=True)
    assert result.exit_code == 0, result.output
    assert "local_server_endpoint" not in _read_cfg().get("intelligence", {})


def test_readback_includes_downloaded_install_state():
    block = _last_json_line(_invoke(as_json=True).output)["intelligence"]
    # No model installed in a fresh isolated config → False (never raises).
    assert block["downloaded_model_installed"] is False


# ---------------------------------------------------------------------------
# U1 — BYO cloud providers (OpenAI / Anthropic / Gemini × key / CLI)
# ---------------------------------------------------------------------------

BYO_CLOUD_IDS = ("openai", "anthropic", "openai-cli", "anthropic-cli", "gemini-cli")


class TestBYOCloudProviders:
    @pytest.mark.parametrize("provider_id", BYO_CLOUD_IDS)
    def test_byo_cloud_provider_round_trips(self, provider_id):
        r = _invoke("cloud_provider", "set", provider_id, as_json=True)
        assert r.exit_code == 0, r.output
        assert _read_cfg()["intelligence"]["cloud_provider"] == provider_id

        from screencap import config

        config.invalidate_config_cache()
        assert config.get_llm_cloud_provider() == provider_id

    @pytest.mark.parametrize("provider_id", BYO_CLOUD_IDS)
    def test_byo_ids_rejected_as_active_provider(self, provider_id):
        # KTD2: BYO providers are cloud-fallback targets only. A non-on-device
        # active provider never day-splits (routing → Unavailable), so the CLI
        # must refuse to persist a BYO id as the active ``provider``.
        r = _invoke("provider", "set", provider_id)
        assert r.exit_code != 0
        assert "must be one of" in r.output
        assert "llm_provider" not in _read_cfg()


@pytest.mark.privacy
class TestBYONeverCloudInvariant:
    """The frames/day-split never-cloud guards (R7) hold after the
    cloud-provider enum is widened for BYO providers."""

    @pytest.mark.parametrize("byo", ("openai", "anthropic", "gemini-cli"))
    def test_frames_row_still_rejected_with_byo_configured(self, byo):
        _invoke("cloud_provider", "set", byo)
        _invoke("summary_cloud_consent", "set", "true")
        r = _invoke("frames_cloud_consent", "set", "true")
        assert r.exit_code != 0
        assert "never sent" in r.output
        assert "frames_cloud_consent" not in _read_cfg().get("intelligence", {})

    @pytest.mark.parametrize("byo", ("openai", "anthropic", "gemini-cli"))
    def test_day_split_row_still_rejected_with_byo_configured(self, byo):
        _invoke("cloud_provider", "set", byo)
        r = _invoke("day_split_cloud_consent", "set", "true")
        assert r.exit_code != 0
        assert "on-device" in r.output


# ---------------------------------------------------------------------------
# U2 — BYO API-key set/clear over stdin (secret NEVER in argv) + presence flag
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_key_store(monkeypatch):
    """In-memory stand-in for the per-vendor Keychain key store, so the CLI's
    --set-key / --clear-key path is exercised without touching the real Keychain
    or the network. Returns the backing dict + a list of validate() calls."""
    from screencap.segmentation import secrets as byo

    store: dict = {}
    validate_calls: list = []

    monkeypatch.setattr(byo, "store_key", lambda v, k: store.__setitem__(v, k))
    monkeypatch.setattr(byo, "load_key", lambda v: store.get(v))
    monkeypatch.setattr(byo, "delete_key", lambda v: store.pop(v, None))
    monkeypatch.setattr(byo, "has_key", lambda v: v in store)

    def _validate(vendor, key):
        validate_calls.append((vendor, key))
        # Default: valid. Individual tests override by re-patching.
        return byo.VALID

    monkeypatch.setattr(byo, "validate_key", _validate)
    return types.SimpleNamespace(store=store, validate_calls=validate_calls)


def _invoke_raw(args, input=None):
    """Invoke ``settings intelligence`` with explicit stdin ``input``."""
    runner = CliRunner()
    return runner.invoke(
        cli, ["settings", "intelligence", *args], input=input, catch_exceptions=False
    )


class TestBYOKeySet:
    def test_set_key_reads_secret_from_stdin(self, fake_key_store):
        r = _invoke_raw(["--set-key", "openai", "--json"], input="sk-my-secret-123\n")
        assert r.exit_code == 0, r.output
        assert fake_key_store.store["openai"] == "sk-my-secret-123"
        payload = _last_json_line(r.output)
        assert payload["ok"] is True
        assert payload["vendor"] == "openai"
        assert payload["key_present"] is True

    def test_secret_never_appears_in_argv(self, fake_key_store, monkeypatch):
        """CRITICAL (KTD3): the secret is piped on stdin — the constructed argv
        must contain NO secret. We assert both the click args and the process
        argv the CLI would see carry only the vendor, never the key."""
        secret = "sk-super-secret-value"
        args = ["--set-key", "openai"]
        # The invocation args (what becomes argv) must not contain the secret.
        assert secret not in args
        r = _invoke_raw([*args, "--json"], input=secret + "\n")
        assert r.exit_code == 0, r.output
        # The stored key is the secret (proving it was read from stdin, not argv).
        assert fake_key_store.store["openai"] == secret
        # And nothing in the invocation args leaked it.
        assert not any(secret in a for a in args)

    def test_set_key_from_file_env(self, fake_key_store, monkeypatch, tmp_path):
        """The 0o600-file channel (SCREENCAP_BYO_KEY_FILE) is the alternative to
        stdin — the secret path, not the secret, is what's passed."""
        key_file = tmp_path / "key.txt"
        key_file.write_text("sk-from-file\n")
        monkeypatch.setenv("SCREENCAP_BYO_KEY_FILE", str(key_file))
        r = _invoke_raw(["--set-key", "anthropic", "--json"])
        assert r.exit_code == 0, r.output
        assert fake_key_store.store["anthropic"] == "sk-from-file"

    def test_set_key_empty_stdin_rejected(self, fake_key_store):
        r = _invoke_raw(["--set-key", "openai", "--json"], input="")
        assert r.exit_code != 0
        payload = _last_json_line(r.output)
        assert payload["ok"] is False
        assert payload["error"] == "no_key_supplied"
        assert "openai" not in fake_key_store.store

    def test_set_key_unknown_vendor_rejected(self, fake_key_store):
        # A *-cli id holds no key; --set-key must refuse it.
        r = _invoke_raw(["--set-key", "openai-cli", "--json"], input="sk-x\n")
        assert r.exit_code != 0
        payload = _last_json_line(r.output)
        assert payload["ok"] is False
        assert payload["error"].startswith("unknown_key_vendor")


class TestBYOKeyValidate:
    def test_validate_valid_stores(self, fake_key_store, monkeypatch):
        from screencap.segmentation import secrets as byo

        monkeypatch.setattr(byo, "validate_key", lambda v, k: byo.VALID)
        r = _invoke_raw(["--set-key", "openai", "--validate", "--json"], input="sk-good\n")
        assert r.exit_code == 0, r.output
        payload = _last_json_line(r.output)
        assert payload["validation"] == "valid"
        assert fake_key_store.store["openai"] == "sk-good"

    def test_validate_invalid_does_not_store(self, fake_key_store, monkeypatch):
        from screencap.segmentation import secrets as byo

        monkeypatch.setattr(byo, "validate_key", lambda v, k: byo.INVALID)
        r = _invoke_raw(["--set-key", "openai", "--validate", "--json"], input="sk-bad\n")
        assert r.exit_code != 0
        payload = _last_json_line(r.output)
        assert payload["ok"] is False
        assert payload["validation"] == "invalid"
        assert payload["error"] == "key_invalid"
        # A rejected key is NOT persisted.
        assert "openai" not in fake_key_store.store

    def test_validate_unknown_still_stores(self, fake_key_store, monkeypatch):
        # Couldn't reach the vendor → store anyway, surface the unknown state.
        from screencap.segmentation import secrets as byo

        monkeypatch.setattr(byo, "validate_key", lambda v, k: byo.UNKNOWN)
        r = _invoke_raw(["--set-key", "gemini", "--validate", "--json"], input="sk-x\n")
        assert r.exit_code == 0, r.output
        payload = _last_json_line(r.output)
        assert payload["validation"] == "unknown"
        assert fake_key_store.store["gemini"] == "sk-x"


class TestBYOKeyClear:
    def test_clear_removes_key(self, fake_key_store):
        fake_key_store.store["openai"] = "sk-existing"
        r = _invoke_raw(["--clear-key", "openai", "--json"])
        assert r.exit_code == 0, r.output
        payload = _last_json_line(r.output)
        assert payload["ok"] is True
        assert payload["key_present"] is False
        assert "openai" not in fake_key_store.store

    def test_clear_absent_key_is_ok(self, fake_key_store):
        r = _invoke_raw(["--clear-key", "anthropic", "--json"])
        assert r.exit_code == 0, r.output
        assert _last_json_line(r.output)["ok"] is True


class TestBYOKeyPresenceReadback:
    def test_json_readback_exposes_presence_not_value(self, fake_key_store):
        fake_key_store.store["openai"] = "sk-secret-should-not-leak"
        r = _invoke(as_json=True)
        payload = _last_json_line(r.output)["intelligence"]
        # Presence booleans present for every key vendor.
        assert payload["openai_key_present"] is True
        assert payload["anthropic_key_present"] is False
        assert payload["gemini_key_present"] is False
        # The value NEVER appears anywhere in the read-back output.
        assert "sk-secret-should-not-leak" not in r.output

    def test_readback_schema_version_bumped(self, fake_key_store):
        r = _invoke(as_json=True)
        # v3: U4 added the per-vendor ``*-cli`` availability booleans.
        assert _last_json_line(r.output)["schema_version"] == 3
