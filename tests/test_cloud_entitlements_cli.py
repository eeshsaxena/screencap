"""Client-side founding-grant + cloud CLI + settings routing (U5/U7 client seam).

Covers:
- ``screencap.entitlements.grant_founding`` (the client half): POST the
  grant-founding action, then re-read via the force-refreshing get_entitlements;
  error mapping for signed-out / service failure.
- the ``screencap cloud grant`` / ``cloud entitlements`` CLI envelopes.
- ``settings --set upload_default`` / ``training_contribution`` routing through
  the U5 config setters, and the read-side ``settings --json`` exposure.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest
from click.testing import CliRunner

from screencap.cli import cli


# ---------------------------------------------------------------------------
# Client grant_founding()
# ---------------------------------------------------------------------------


@pytest.mark.privacy
def test_grant_founding_success(monkeypatch):
    from screencap import auth
    from screencap import entitlements as ent

    resp = mock.MagicMock(status_code=200)
    resp.json.return_value = {"entitlement": {"plan": "founding", "active": True, "expires": None}}
    monkeypatch.setattr("screencap.auth.get_id_token", lambda force_refresh=False: "tok")
    monkeypatch.setattr("screencap.entitlements.requests.post", lambda *a, **k: resp)
    # After the grant, the entitlement is re-read via the force-refreshing seam.
    monkeypatch.setattr(
        auth, "get_entitlements",
        lambda: {"plan": "founding", "active": True, "expires": None},
    )
    assert ent.grant_founding() == {"plan": "founding", "active": True, "expires": None}


@pytest.mark.privacy
def test_grant_founding_not_signed_in_maps_to_sign_in_message(monkeypatch):
    from screencap import auth
    from screencap import entitlements as ent

    def _not_signed_in(force_refresh=False):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.auth.get_id_token", _not_signed_in)
    with pytest.raises(RuntimeError, match="Sign in"):
        ent.grant_founding()


@pytest.mark.privacy
def test_grant_founding_service_error_maps_to_setup_failed(monkeypatch):
    from screencap import entitlements as ent

    resp = mock.MagicMock(status_code=500)
    resp.json.return_value = {"error": "boom"}
    resp.text = "boom"
    monkeypatch.setattr("screencap.auth.get_id_token", lambda force_refresh=False: "tok")
    monkeypatch.setattr("screencap.entitlements.requests.post", lambda *a, **k: resp)
    with pytest.raises(RuntimeError, match="Could not set up cloud"):
        ent.grant_founding()


# ---------------------------------------------------------------------------
# `screencap cloud grant` / `cloud entitlements` CLI envelopes
# ---------------------------------------------------------------------------


@pytest.mark.privacy
def test_cloud_grant_json_envelope_success(monkeypatch):
    monkeypatch.setattr(
        "screencap.entitlements.grant_founding",
        lambda: {"plan": "founding", "active": True, "expires": None},
    )
    res = CliRunner().invoke(cli, ["cloud", "grant", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["ok"] is True
    assert payload["plan"] == "founding"
    assert payload["active"] is True
    assert payload["expires"] is None


@pytest.mark.privacy
def test_cloud_grant_json_envelope_failure_exit_1(monkeypatch):
    def _boom():
        raise RuntimeError("Sign in first: run `screencap login`.")

    monkeypatch.setattr("screencap.entitlements.grant_founding", _boom)
    res = CliRunner().invoke(cli, ["cloud", "grant", "--json"])
    assert res.exit_code == 1
    payload = json.loads(res.output)
    assert payload["ok"] is False
    assert "Sign in" in payload["error"]


@pytest.mark.privacy
def test_cloud_entitlements_json_envelope(monkeypatch):
    monkeypatch.setattr(
        "screencap.auth.get_entitlements",
        lambda: {"plan": "free", "active": False, "expires": None},
    )
    res = CliRunner().invoke(cli, ["cloud", "entitlements", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["ok"] is True
    assert payload["plan"] == "free"
    assert payload["active"] is False


# ---------------------------------------------------------------------------
# settings --set routing through the U5 config setters
# ---------------------------------------------------------------------------


def _settings_set(tmp_path, monkeypatch, key_value):
    import screencap.config as cfg

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg, "_CONFIG_PATH", cfg_path)
    cfg._config_cache = None
    res = CliRunner().invoke(cli, ["settings", "--set", key_value])
    return res, cfg_path


def test_settings_set_upload_default_routes_through_setter(tmp_path, monkeypatch):
    # The set path writes unconditionally (env only affects reads), so the file
    # content is the assertion.
    res, cfg_path = _settings_set(tmp_path, monkeypatch, "upload_default=cloud")
    assert res.exit_code == 0
    text = cfg_path.read_text()
    assert "[privacy]" in text
    assert 'upload_default = "cloud"' in text


def test_settings_set_training_contribution_routes_through_setter(tmp_path, monkeypatch):
    res, cfg_path = _settings_set(tmp_path, monkeypatch, "training_contribution=true")
    assert res.exit_code == 0
    text = cfg_path.read_text()
    assert "[privacy]" in text
    assert "training_contribution = true" in text


def test_settings_set_upload_default_rejects_invalid(tmp_path, monkeypatch):
    res, _ = _settings_set(tmp_path, monkeypatch, "upload_default=evil")
    assert res.exit_code != 0


def test_settings_json_exposes_training_contribution(tmp_path, monkeypatch):
    import screencap.config as cfg

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[privacy]\ntraining_contribution = true\n")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(cfg, "get_recordings_dir", lambda: tmp_path)
    cfg._config_cache = None
    env = {k: v for k, v in os.environ.items() if k != "SCREENCAP_TRAINING_CONTRIBUTION"}
    with mock.patch.dict(os.environ, env, clear=True):
        res = CliRunner().invoke(cli, ["settings", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["settings"]["training_contribution"] is True
