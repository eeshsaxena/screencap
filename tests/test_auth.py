"""Tests for the client auth module (screencap.auth).

Network (Google token / signInWithIdp / secure-token refresh) and the macOS
Keychain are both mocked, so these run fully offline. The most subtle surface is
``get_id_token`` refresh/rotation, so that is covered most heavily.
"""

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

import screencap.auth as a
from tests._jwt import _jwt
from tests.conftest import _fake_provisioned, _install_provisioned

# --------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch, tmp_path):
    """Reset the in-memory token cache and redirect the cross-process lock file
    to a tmp path so tests never touch ~/.screencap/run."""
    a._cached = None
    monkeypatch.setattr(a, "_lock_path", lambda: tmp_path / "auth.lock")
    yield
    a._cached = None


@pytest.fixture
def fake_keyring(monkeypatch):
    """In-memory stand-in for the macOS Keychain, in the **un-entitled** context.

    The access-group backend is forced to raise ``MissingEntitlement`` so every
    ``auth`` wrapper falls back to this ``keyring`` stand-in deterministically and
    without touching the real Keychain (SCR-241 Fork 1A). Entitled-path behavior
    is covered separately by the ``fake_group`` fixture.
    """
    import keyring
    import keyring.errors

    from screencap import keychain_group

    store = {}

    def setp(service, account, pw):
        store[(service, account)] = pw

    def getp(service, account):
        return store.get((service, account))

    def delp(service, account):
        if (service, account) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, account)]

    monkeypatch.setattr(keyring, "set_password", setp)
    monkeypatch.setattr(keyring, "get_password", getp)
    monkeypatch.setattr(keyring, "delete_password", delp)

    def _missing(*_a, **_k):
        raise keychain_group.MissingEntitlement(keychain_group.errSecMissingEntitlement, "test")

    monkeypatch.setattr(keychain_group, "store", _missing)
    monkeypatch.setattr(keychain_group, "load", _missing)
    monkeypatch.setattr(keychain_group, "delete", _missing)
    return store


@pytest.fixture
def fake_group(monkeypatch):
    """The **entitled** context: the access-group backend succeeds (in-memory) and
    ``keyring`` is a spy so tests can assert it is NOT used on the group path.

    Forces ``sys.platform == 'darwin'`` so the wrappers take the access-group
    branch regardless of the host running the test.
    """
    import keyring

    from screencap import keychain_group

    monkeypatch.setattr(a.sys, "platform", "darwin")
    group: dict = {}
    legacy: dict = {}  # a simulated pre-existing legacy item, for migration tests
    keyring_calls: list = []

    monkeypatch.setattr(keychain_group, "store", lambda s, ac, sec, g: group.__setitem__((s, ac, g), sec))
    monkeypatch.setattr(keychain_group, "load", lambda s, ac, g: group.get((s, ac, g)))
    monkeypatch.setattr(keychain_group, "delete", lambda s, ac, g: group.pop((s, ac, g), None))
    monkeypatch.setattr(keychain_group, "load_legacy_noninteractive", lambda s, ac: legacy.get((s, ac)))
    monkeypatch.setattr(
        keychain_group,
        "delete_legacy_noninteractive",
        lambda s, ac: (legacy.pop((s, ac), None) is not None) or True,
    )

    monkeypatch.setattr(keyring, "set_password", lambda s, ac, pw: keyring_calls.append(("set", s, ac)))
    monkeypatch.setattr(keyring, "get_password", lambda s, ac: keyring_calls.append(("get", s, ac)) or None)
    monkeypatch.setattr(keyring, "delete_password", lambda s, ac: keyring_calls.append(("del", s, ac)))
    return types.SimpleNamespace(group=group, legacy=legacy, keyring_calls=keyring_calls)


_KEY = (a.KEYCHAIN_SERVICE, a.KEYCHAIN_ACCOUNT)
_GROUP_KEY = (a.KEYCHAIN_SERVICE, a.KEYCHAIN_ACCOUNT, a.KEYCHAIN_ACCESS_GROUP)


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


# --------------------------------------------------------------------------
# get_id_token — refresh / expiry / rotation (the subtle part)
# --------------------------------------------------------------------------


def test_cached_token_returned_when_fresh(monkeypatch):
    a._cached = a.AuthState(id_token="cached", refresh_token="r", expires_at=time.time() + 3600, uid="u")

    def explode(*_):
        raise AssertionError("should not refresh a fresh token")

    monkeypatch.setattr(a, "_refresh", explode)
    assert a.get_id_token() == "cached"


def test_refreshes_within_buffer_and_persists_rotation(fake_keyring, monkeypatch):
    fake_keyring[_KEY] = "old-refresh"
    # Cached token is within the 5-min refresh buffer -> must refresh.
    a._cached = a.AuthState(id_token="old", refresh_token="old-refresh", expires_at=time.time() + 60, uid="u")

    def fake_post(url, params=None, data=None, timeout=None):
        assert "securetoken" in url
        assert data["refresh_token"] == "old-refresh"
        return _FakeResp(200, {
            "id_token": _jwt({"user_id": "u", "email": "e@x.com"}),
            "refresh_token": "new-refresh",
            "expires_in": "3600",
            "user_id": "u",
        })

    monkeypatch.setattr(a.requests, "post", fake_post)
    token = a.get_id_token()
    assert token  # fresh ID token returned
    assert a._cached.refresh_token == "new-refresh"
    assert fake_keyring[_KEY] == "new-refresh"  # rotation persisted to Keychain


def test_no_stored_token_raises_not_signed_in(fake_keyring):
    with pytest.raises(a.NotSignedIn):
        a.get_id_token()


def test_refresh_rejected_raises_not_signed_in(fake_keyring, monkeypatch):
    fake_keyring[_KEY] = "bad-refresh"
    monkeypatch.setattr(a.requests, "post", lambda *x, **k: _FakeResp(400, {"error": "TOKEN_EXPIRED"}))
    with pytest.raises(a.NotSignedIn):
        a.get_id_token()


def test_refresh_network_error_raises_autherror_not_notsignedin(fake_keyring, monkeypatch):
    # A transient network failure must NOT be mistaken for "signed out".
    fake_keyring[_KEY] = "rt"

    def boom(*x, **k):
        raise a.requests.ConnectionError("offline")

    monkeypatch.setattr(a.requests, "post", boom)
    with pytest.raises(a.AuthError) as exc:
        a.get_id_token()
    assert not isinstance(exc.value, a.NotSignedIn)


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


def test_login_persists_refresh_token(fake_keyring, monkeypatch):
    monkeypatch.setattr(a, "_login_via_browser", lambda timeout, open_browser: ("code", "http://127.0.0.1:1234", "verifier"))
    monkeypatch.setattr(a, "_exchange_code_for_google_token", lambda code, v, r: "gtoken")
    monkeypatch.setattr(
        a, "_sign_in_with_idp",
        lambda g, r: a.AuthState(id_token="id", refresh_token="rt", expires_at=time.time() + 3600, uid="uid1", email="e@x.com"),
    )
    state = a.login(open_browser=False)
    assert state.uid == "uid1"
    assert fake_keyring[_KEY] == "rt"
    assert a._cached.id_token == "id"


def test_validate_callback_happy():
    assert a._validate_callback("code", "s", None, "s") == "code"


def test_validate_callback_state_mismatch_aborts():
    with pytest.raises(a.AuthError):
        a._validate_callback("code", "wrong", None, "expected")


def test_validate_callback_provider_error_aborts():
    with pytest.raises(a.AuthError):
        a._validate_callback(None, None, "access_denied", "expected")


def test_validate_callback_missing_code_aborts():
    with pytest.raises(a.AuthError):
        a._validate_callback(None, "expected", None, "expected")


def test_sign_in_with_idp_parses_firebase_tokens(monkeypatch):
    monkeypatch.setattr(
        a.requests, "post",
        lambda *x, **k: _FakeResp(200, {
            "idToken": "id", "refreshToken": "rt", "localId": "uid1",
            "email": "e@x.com", "expiresIn": "3600",
        }),
    )
    st = a._sign_in_with_idp("gtok", "http://127.0.0.1:1")
    assert st.uid == "uid1"
    assert st.email == "e@x.com"
    assert st.refresh_token == "rt"


def test_describe_error_surfaces_oauth_error_description():
    # Google's token endpoint returns a *string* `error` plus an `error_description`
    # carrying the actionable reason. Dropping the description is what turned
    # "client_secret is missing." into a bare, undiagnosable "invalid_request".
    resp = _FakeResp(
        400, {"error": "invalid_request", "error_description": "client_secret is missing."}
    )
    msg = a._describe_error(resp)
    assert "client_secret is missing." in msg
    assert "invalid_request" in msg


def test_describe_error_handles_identitytoolkit_dict_shape():
    # Firebase Identity Toolkit nests the reason under error.message; the dict
    # branch must keep returning it, unaffected by the OAuth-shape fix.
    resp = _FakeResp(400, {"error": {"code": 400, "message": "INVALID_IDP_RESPONSE"}})
    assert a._describe_error(resp) == "INVALID_IDP_RESPONSE"


# --------------------------------------------------------------------------
# logout + whoami
# --------------------------------------------------------------------------


def test_logout_deletes_and_whoami_reports_signed_out(fake_keyring, monkeypatch):
    monkeypatch.setattr("screencap.config.get_stripe_paywall_enabled", lambda: False)
    fake_keyring[_KEY] = "rt"
    assert a.logout() is True
    assert _KEY not in fake_keyring
    assert a.whoami() == {"signed_in": False, "paywall_enabled": False}


def test_logout_when_not_signed_in_returns_false(fake_keyring):
    assert a.logout() is False


def test_whoami_signed_in_reads_subscribed_claim(fake_keyring, monkeypatch):
    monkeypatch.setattr("screencap.config.get_stripe_paywall_enabled", lambda: False)
    fake_keyring[_KEY] = "rt"
    tok = _jwt({"user_id": "uid1", "email": "e@x.com", "subscribed": True})
    monkeypatch.setattr(
        a, "_ensure_fresh",
        lambda: a.AuthState(tok, "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    assert a.whoami() == {
        "signed_in": True,
        "uid": "uid1",
        "email": "e@x.com",
        "subscribed": True,
        # U6: `tier` now rides every signed-in envelope; absent claim -> None.
        "tier": None,
        "paywall_enabled": False,
    }


def test_whoami_surfaces_paywall_flag_in_every_state(fake_keyring, monkeypatch):
    """The client paywall flag (KTD-6) rides EVERY whoami envelope — signed-out
    included — so the app can gate pricing/soft-gate before sign-in."""
    monkeypatch.setattr("screencap.config.get_stripe_paywall_enabled", lambda: True)
    # Signed out (no refresh token stored).
    assert a.whoami() == {"signed_in": False, "paywall_enabled": True}
    # Signed in.
    fake_keyring[_KEY] = "rt"
    tok = _jwt({"user_id": "uid1", "email": "e@x.com"})
    monkeypatch.setattr(
        a, "_ensure_fresh",
        lambda: a.AuthState(tok, "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    assert a.whoami()["paywall_enabled"] is True


def test_whoami_signed_in_absent_claim_is_unsubscribed(fake_keyring, monkeypatch):
    fake_keyring[_KEY] = "rt"
    tok = _jwt({"user_id": "uid1", "email": "e@x.com"})
    monkeypatch.setattr(
        a, "_ensure_fresh",
        lambda: a.AuthState(tok, "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    assert a.whoami()["subscribed"] is False


def test_whoami_offline_reports_stale_not_crash(fake_keyring, monkeypatch):
    fake_keyring[_KEY] = "rt"

    def boom():
        raise a.AuthError("offline")

    monkeypatch.setattr(a, "_ensure_fresh", boom)
    info = a.whoami()
    assert info["signed_in"] is True
    assert info.get("stale") is True


# --------------------------------------------------------------------------
# whoami — two-tier `tier` + `trial_end` claims (U6, KTD-1/KTD-4)
# --------------------------------------------------------------------------


def test_whoami_cloud_tier_claim(fake_keyring, monkeypatch):
    """A ``tier=cloud`` token surfaces ``tier: cloud`` with ``subscribed: True``
    and the ``trial_end`` written by the webhook while trialing (U2)."""
    monkeypatch.setattr("screencap.config.get_stripe_paywall_enabled", lambda: False)
    fake_keyring[_KEY] = "rt"
    tok = _jwt(
        {
            "user_id": "uid1",
            "email": "e@x.com",
            "subscribed": True,
            "tier": "cloud",
            "trial_end": 1_800_000_000,
        }
    )
    monkeypatch.setattr(
        a, "_ensure_fresh",
        lambda: a.AuthState(tok, "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    info = a.whoami()
    assert info["tier"] == "cloud"
    assert info["subscribed"] is True
    assert info["trial_end"] == 1_800_000_000


def test_whoami_local_tier_claim_not_subscribed(fake_keyring, monkeypatch):
    """A ``tier=local`` token surfaces ``tier: local`` and ``subscribed`` stays
    False — the local-entitled user must never pass the cloud gate (KTD-1)."""
    monkeypatch.setattr("screencap.config.get_stripe_paywall_enabled", lambda: False)
    fake_keyring[_KEY] = "rt"
    tok = _jwt({"user_id": "uid1", "email": "e@x.com", "subscribed": False, "tier": "local"})
    monkeypatch.setattr(
        a, "_ensure_fresh",
        lambda: a.AuthState(tok, "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    info = a.whoami()
    assert info["tier"] == "local"
    assert info["subscribed"] is False
    # No trial in flight -> the key is absent (not a spurious None).
    assert "trial_end" not in info


def test_whoami_absent_tier_is_none(fake_keyring, monkeypatch):
    """A token carrying no ``tier`` claim yields ``tier: None`` (fresh
    not-entitled) WITHOUT a ``stale`` flag — distinct from the offline path."""
    monkeypatch.setattr("screencap.config.get_stripe_paywall_enabled", lambda: False)
    fake_keyring[_KEY] = "rt"
    tok = _jwt({"user_id": "uid1", "email": "e@x.com"})
    monkeypatch.setattr(
        a, "_ensure_fresh",
        lambda: a.AuthState(tok, "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    info = a.whoami()
    assert info["tier"] is None
    assert info["subscribed"] is False
    assert info.get("stale") is not True


def test_whoami_offline_tier_none_with_stale(fake_keyring, monkeypatch):
    """CRITICAL (KTD-4): the offline/AuthError branch returns ``tier: None``
    **with** ``stale: True`` so a downstream gate distinguishes
    paying-but-offline (grace via the lease) from genuinely not-entitled — never
    via tier presence, always via the ``stale`` flag."""
    monkeypatch.setattr("screencap.config.get_stripe_paywall_enabled", lambda: False)
    fake_keyring[_KEY] = "rt"

    def boom():
        raise a.AuthError("offline")

    monkeypatch.setattr(a, "_ensure_fresh", boom)
    info = a.whoami()
    assert info["signed_in"] is True
    assert info["stale"] is True
    assert info["tier"] is None


def test_pkce_challenge_is_url_safe_sha256():
    verifier = a._gen_code_verifier()
    challenge = a._code_challenge(verifier)
    assert "=" not in challenge and "+" not in challenge and "/" not in challenge
    assert 43 <= len(verifier) <= 128


# --------------------------------------------------------------------------
# CLI envelope — whoami --json in both states
# --------------------------------------------------------------------------


def test_cli_whoami_json_envelope_both_states(monkeypatch):
    from click.testing import CliRunner

    from screencap.cli import _AUTH_SCHEMA_VERSION, whoami_cmd

    runner = CliRunner()

    monkeypatch.setattr(a, "whoami", lambda: {"signed_in": False})
    res = runner.invoke(whoami_cmd, ["--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["ok"] is True
    assert payload["schema_version"] == _AUTH_SCHEMA_VERSION
    assert payload["signed_in"] is False

    monkeypatch.setattr(
        a, "whoami",
        lambda: {
            "signed_in": True,
            "uid": "u",
            "email": "e@x.com",
            "subscribed": True,
            "tier": "cloud",
        },
    )
    res = runner.invoke(whoami_cmd, ["--json"])
    payload = json.loads(res.output)
    assert payload["signed_in"] is True
    assert payload["email"] == "e@x.com"
    assert payload["subscribed"] is True
    # U6: the CLI envelope spreads whoami() wholesale, so `tier` rides through.
    assert payload["tier"] == "cloud"


def test_cli_whoami_force_refresh_remints_token(monkeypatch):
    from click.testing import CliRunner

    from screencap.cli import whoami_cmd

    calls = {}

    def fake_get_id_token(force_refresh=False):
        calls["force"] = force_refresh
        return "tok"

    monkeypatch.setattr(a, "get_id_token", fake_get_id_token)
    monkeypatch.setattr(
        a, "whoami", lambda: {"signed_in": True, "uid": "u", "subscribed": True}
    )
    res = CliRunner().invoke(whoami_cmd, ["--json", "--force-refresh"])
    assert res.exit_code == 0
    assert calls.get("force") is True  # re-mint was forced before reading state


# --------------------------------------------------------------------------
# Review fixes (PR #210)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_refresh_transient_status_is_autherror_not_notsignedin(fake_keyring, monkeypatch, status):
    # #6: 429 / 5xx are transient — the refresh token is still valid, so the
    # caller gets a retryable AuthError, not a forced re-login.
    fake_keyring[_KEY] = "rt"
    monkeypatch.setattr(a.requests, "post", lambda *x, **k: _FakeResp(status, {"error": "transient"}))
    with pytest.raises(a.AuthError) as exc:
        a.get_id_token()
    assert not isinstance(exc.value, a.NotSignedIn)


@pytest.mark.parametrize("status", [400, 401])
def test_refresh_4xx_status_is_notsignedin(fake_keyring, monkeypatch, status):
    fake_keyring[_KEY] = "rt"
    monkeypatch.setattr(a.requests, "post", lambda *x, **k: _FakeResp(status, {"error": "bad"}))
    with pytest.raises(a.NotSignedIn):
        a.get_id_token()


def test_ensure_fresh_prefers_keychain_over_stale_cached(fake_keyring, monkeypatch):
    # #3: a long-lived process holding a stale in-memory token must refresh with
    # the Keychain value (the rotation source of truth), not the stale one.
    fake_keyring[_KEY] = "RT_new"
    a._cached = a.AuthState(id_token="old", refresh_token="RT_old", expires_at=time.time() + 60, uid="u")
    used = {}

    def fake_refresh(rt):
        used["rt"] = rt
        return a.AuthState(id_token="new", refresh_token=rt, expires_at=time.time() + 3600, uid="u")

    monkeypatch.setattr(a, "_refresh", fake_refresh)
    a.get_id_token()
    assert used["rt"] == "RT_new"


def test_ensure_fresh_retries_with_rotated_keychain_token(fake_keyring, monkeypatch):
    # #3: if the token we sent was rejected but another process rotated it
    # mid-refresh, reload and retry once before forcing a logout.
    fake_keyring[_KEY] = "RT1"
    a._cached = None
    calls = []

    def fake_refresh(rt):
        calls.append(rt)
        if rt == "RT1":
            fake_keyring[_KEY] = "RT2"  # another process rotated during our refresh
            raise a.NotSignedIn("rejected")
        return a.AuthState(id_token="id", refresh_token=rt, expires_at=time.time() + 3600, uid="u")

    monkeypatch.setattr(a, "_refresh", fake_refresh)
    a.get_id_token()
    assert calls == ["RT1", "RT2"]


def test_loopback_handler_ignores_non_oauth_probe_then_accepts_callback():
    # #10: a favicon/preconnect probe must not consume the single callback. The
    # handler 404s non-OAuth requests (recording nothing) and only records a real
    # code/error.
    import http.client
    import threading
    from http.server import HTTPServer

    from screencap.auth import _CallbackHandler

    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.callback_code = server.callback_state = server.callback_error = None
    server.callback_received = False
    port = server.server_address[1]
    try:
        # 1. non-OAuth probe -> 404, nothing recorded
        t = threading.Thread(target=server.handle_request)
        t.start()
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/favicon.ico")
        r = c.getresponse()
        r.read()
        t.join(timeout=5)
        assert r.status == 404
        assert server.callback_received is False
        # 2. the genuine callback -> 200, code captured
        t = threading.Thread(target=server.handle_request)
        t.start()
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/?code=abc&state=xyz")
        r = c.getresponse()
        r.read()
        t.join(timeout=5)
        assert r.status == 200
        assert server.callback_received is True
        assert server.callback_code == "abc"
        assert server.callback_state == "xyz"
    finally:
        server.server_close()


def test_logout_returns_true_when_delete_fails_but_token_present(fake_keyring, monkeypatch):
    # #11: a Keychain delete failure (locked / backend error, not "nothing
    # stored") must still report that a credential existed, never "already signed
    # out" while the token persists.
    import keyring

    fake_keyring[_KEY] = "rt"

    def boom(service, account):
        raise RuntimeError("Keychain locked")

    monkeypatch.setattr(keyring, "delete_password", boom)
    assert a.logout() is True


def test_cli_login_json_success(monkeypatch):
    from click.testing import CliRunner

    from screencap.cli import login_cmd

    monkeypatch.setattr(
        a, "login",
        lambda: a.AuthState("id", "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    res = CliRunner().invoke(login_cmd, ["--json"])
    assert res.exit_code == 0
    p = json.loads(res.output)
    assert p["ok"] is True and p["signed_in"] is True and p["email"] == "e@x.com"


def test_cli_login_json_failure(monkeypatch):
    from click.testing import CliRunner

    from screencap.cli import login_cmd

    def boom():
        raise a.AuthError("state mismatch")

    monkeypatch.setattr(a, "login", boom)
    res = CliRunner().invoke(login_cmd, ["--json"])
    assert res.exit_code == 1
    p = json.loads(res.output)
    assert p["ok"] is False and "error" in p


def test_cli_whoami_json_stale_state(monkeypatch):
    from click.testing import CliRunner

    from screencap.cli import whoami_cmd

    monkeypatch.setattr(a, "whoami", lambda: {"signed_in": True, "uid": None, "email": None, "stale": True})
    p = json.loads(CliRunner().invoke(whoami_cmd, ["--json"]).output)
    assert p["ok"] is True and p["signed_in"] is True and p["stale"] is True and p["uid"] is None


def test_cli_whoami_json_error_envelope(monkeypatch):
    from click.testing import CliRunner

    from screencap.cli import whoami_cmd

    def boom():
        raise RuntimeError("keychain blew up")

    monkeypatch.setattr(a, "whoami", boom)
    res = CliRunner().invoke(whoami_cmd, ["--json"])
    assert res.exit_code == 1
    p = json.loads(res.output)
    assert p["ok"] is False and "error" in p


def test_cli_login_success_escapes_provider_email_markup(monkeypatch):
    """SCR-169: the ``login`` success line interpolates the provider-controlled
    account email into ``[bold]...[/bold]``. SCR-117 escaped the *error* path but
    left this *success* path. An email carrying an unbalanced ``[/]`` makes Rich
    raise ``MarkupError`` (closing a tag the developer never opened), crashing the
    command after sign-in already succeeded; a balanced ``[x]`` run is silently
    stripped. Same untrusted-source class as PR #216, on the success path.

    Force the human path (CliRunner stdout is non-TTY, which auto-selects the
    unaffected --json path)."""
    from click.testing import CliRunner
    from rich.errors import MarkupError

    import screencap.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_should_default_to_json", lambda: False)
    monkeypatch.setattr(
        a, "login",
        lambda: a.AuthState("id", "rt", time.time() + 3600, "uid1", "ev[/]il@x.com"),
    )
    res = CliRunner().invoke(cli_mod.login_cmd, [])
    assert not isinstance(res.exception, MarkupError), res.exception
    assert res.exit_code == 0
    assert "ev[/]il@x.com" in res.output


def test_cli_whoami_success_escapes_provider_email_markup(monkeypatch):
    """SCR-169: ``whoami`` success interpolates the provider-controlled email/uid
    into ``[bold]{who}[/bold]``. SCR-117 escaped the whoami *error* path but left
    the success path. Markup metacharacters must not crash or be stripped."""
    from click.testing import CliRunner
    from rich.errors import MarkupError

    import screencap.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_should_default_to_json", lambda: False)
    monkeypatch.setattr(
        a, "whoami",
        lambda: {"signed_in": True, "uid": "uid1", "email": "ev[/]il@x.com"},
    )
    res = CliRunner().invoke(cli_mod.whoami_cmd, [])
    assert not isinstance(res.exception, MarkupError), res.exception
    assert res.exit_code == 0
    assert "ev[/]il@x.com" in res.output


# --------------------------------------------------------------------------
# U5: out-of-band engine token + force_refresh
# --------------------------------------------------------------------------


def test_engine_token_file_used_and_keyring_never_read(monkeypatch, tmp_path):
    # In the engine context (env var set) the daemon-supplied file is the ONLY
    # source — get_id_token must never touch keyring or _ensure_fresh.
    token_file = tmp_path / "engine.jwt"
    token_file.write_text("engine-id-token\n")
    monkeypatch.setenv(a.ENGINE_TOKEN_FILE_ENV, str(token_file))

    def explode(*_a, **_k):
        raise AssertionError("engine context must not read keyring / refresh")

    monkeypatch.setattr(a, "_ensure_fresh", explode)
    monkeypatch.setattr(a, "_load_refresh_token", explode)
    assert a.get_id_token() == "engine-id-token"


def test_engine_token_missing_file_raises_not_signed_in_no_keyring_fallback(monkeypatch, tmp_path):
    # env var set but the file is absent → fail closed (NotSignedIn), never fall
    # back to the Keychain path (wrong ACL identity in the engine subprocess).
    monkeypatch.setenv(a.ENGINE_TOKEN_FILE_ENV, str(tmp_path / "absent.jwt"))
    monkeypatch.setattr(a, "_ensure_fresh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fallback")))
    with pytest.raises(a.NotSignedIn):
        a.get_id_token()


def test_engine_token_empty_file_raises_not_signed_in(monkeypatch, tmp_path):
    token_file = tmp_path / "engine.jwt"
    token_file.write_text("   \n")
    monkeypatch.setenv(a.ENGINE_TOKEN_FILE_ENV, str(token_file))
    with pytest.raises(a.NotSignedIn):
        a.get_id_token()


def test_engine_token_read_fresh_each_call_picks_up_remint(monkeypatch, tmp_path):
    # The daemon re-mints + rewrites the file on a timer; the engine must read it
    # fresh so a long recording picks up the new token without a restart.
    token_file = tmp_path / "engine.jwt"
    token_file.write_text("token-A")
    monkeypatch.setenv(a.ENGINE_TOKEN_FILE_ENV, str(token_file))
    assert a.get_id_token() == "token-A"
    token_file.write_text("token-B")  # daemon re-mint
    assert a.get_id_token() == "token-B"


def test_force_refresh_remints_even_when_cached_is_fresh(fake_keyring, monkeypatch):
    # The cloud paths' 401-retry forces a re-mint even though the cached ID token
    # still looks fresh (server rejected it: skew / rotation / revocation).
    fake_keyring[_KEY] = "rt"
    a._cached = a.AuthState(id_token="stale-but-unexpired", refresh_token="rt", expires_at=time.time() + 3600, uid="u")
    calls = []

    def fake_refresh(rt):
        calls.append(rt)
        return a.AuthState(id_token="reminted", refresh_token=rt, expires_at=time.time() + 3600, uid="u")

    monkeypatch.setattr(a, "_refresh", fake_refresh)
    assert a.get_id_token() == "stale-but-unexpired"  # default: cache honored
    assert calls == []
    assert a.get_id_token(force_refresh=True) == "reminted"  # forced re-mint
    assert calls == ["rt"]


def test_authed_post_attaches_bearer_and_returns_response(monkeypatch):
    monkeypatch.setattr(a, "get_id_token", lambda force_refresh=False: "tok")
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen["url"], seen["headers"], seen["timeout"] = url, headers, timeout
        return _FakeResp(200, {"ok": True})

    resp = a.authed_post(post, "https://fn", json={"a": 1}, timeout=30)
    assert resp.status_code == 200
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["url"] == "https://fn" and seen["timeout"] == 30


def test_authed_post_refreshes_and_retries_once_on_401(monkeypatch):
    forces = []
    monkeypatch.setattr(a, "get_id_token", lambda force_refresh=False: forces.append(force_refresh) or ("fresh" if force_refresh else "stale"))
    responses = [_FakeResp(401, {}), _FakeResp(200, {"ok": True})]
    headers_seen = []

    def post(url, json=None, headers=None, timeout=None):
        headers_seen.append(headers["Authorization"])
        return responses.pop(0)

    resp = a.authed_post(post, "https://fn")
    assert resp.status_code == 200
    assert forces == [False, True]  # second attempt forced a refresh
    assert headers_seen == ["Bearer stale", "Bearer fresh"]


def test_authed_post_propagates_not_signed_in_before_any_post(monkeypatch):
    def not_signed_in(force_refresh=False):
        raise a.NotSignedIn("no creds")

    monkeypatch.setattr(a, "get_id_token", not_signed_in)
    called = []
    with pytest.raises(a.NotSignedIn):
        a.authed_post(lambda *x, **k: called.append(1), "https://fn")
    assert called == []  # never posts without a token


# --------------------------------------------------------------------------
# id_token_uid — client-side uid extraction for the SCR-116 ownership gate
# --------------------------------------------------------------------------


def test_id_token_uid_prefers_user_id_then_sub():
    assert a.id_token_uid(_jwt({"user_id": "A", "sub": "B"})) == "A"
    assert a.id_token_uid(_jwt({"sub": "B"})) == "B"


def test_id_token_uid_returns_none_for_junk_or_empty():
    # Malformed / non-JWT / empty all yield None (caller treats unknown uid as
    # "cannot confirm ownership" and falls back to current behavior).
    assert a.id_token_uid("not-a-jwt") is None
    assert a.id_token_uid("") is None
    assert a.id_token_uid(_jwt({"email": "e@x.com"})) is None  # no uid claim


# --------------------------------------------------------------------------
# Credential resolution: env var > injected _provisioned > placeholder (U1)
#
# The release binary carries provisioned values via a gitignored
# screencap._provisioned module (written by scripts/generate_provisioned.py).
# These tests pin the three-layer precedence and the fail-loud-on-malformed
# contract without writing _provisioned.py to disk (the suite must pass with no
# such file present).
# --------------------------------------------------------------------------


@pytest.fixture
def _no_cred_env(monkeypatch):
    """Clear the explicit credential env overrides so the lower layers show."""
    monkeypatch.delenv("SCREENCAP_FIREBASE_API_KEY", raising=False)
    monkeypatch.delenv("SCREENCAP_OAUTH_CLIENT_ID", raising=False)


def test_resolvers_return_provisioned_when_present_and_no_env(monkeypatch, _no_cred_env):
    _install_provisioned(
        monkeypatch,
        _fake_provisioned(FIREBASE_API_KEY="prov-key", OAUTH_CLIENT_ID="prov-id"),
    )
    assert a._api_key() == "prov-key"
    assert a._oauth_client_id() == "prov-id"


def test_resolvers_fall_back_to_placeholder_when_provisioned_absent(monkeypatch, _no_cred_env):
    _install_provisioned(monkeypatch, None)
    # No ImportError surfaces — an absent module is the normal source-checkout state.
    assert a._api_key() == a.DEFAULT_FIREBASE_API_KEY
    assert a._oauth_client_id() == a.DEFAULT_OAUTH_CLIENT_ID


@pytest.mark.parametrize(
    "present_attr, resolver",
    [
        ("OAUTH_CLIENT_ID", lambda: a._api_key()),          # FIREBASE_API_KEY missing
        ("FIREBASE_API_KEY", lambda: a._oauth_client_id()),  # OAUTH_CLIENT_ID missing
    ],
)
def test_malformed_provisioned_raises_rather_than_silent_placeholder(
    monkeypatch, _no_cred_env, present_attr, resolver
):
    # Present module missing the expected constant → AttributeError (getattr has no
    # default), NOT a silent degrade to the placeholder the build guard might ship.
    # Covered in both directions so neither resolver can regress to a quiet fallback.
    _install_provisioned(monkeypatch, _fake_provisioned(**{present_attr: "prov-val"}))
    with pytest.raises(AttributeError):
        resolver()


def test_syntax_error_in_provisioned_propagates_not_silent_placeholder(
    monkeypatch, _no_cred_env
):
    # A PRESENT but syntactically-invalid _provisioned module must NOT be swallowed:
    # the catch in _provisioned_value is `except ImportError` only, and SyntaxError is
    # not an ImportError subclass — so it propagates loudly rather than degrading to a
    # placeholder the build guard might then ship as a broken-sign-in binary.
    import importlib.abc
    import importlib.machinery

    import screencap

    class _SyntaxErrorLoader(importlib.abc.Loader):
        def create_module(self, spec):
            return None  # default module creation

        def exec_module(self, module):
            raise SyntaxError("invalid syntax in _provisioned.py")

    class _SyntaxErrorFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "screencap._provisioned":
                return importlib.machinery.ModuleSpec(fullname, _SyntaxErrorLoader())
            return None

    # Clear any cached module + package attribute so the import reaches our finder
    # (monkeypatch reverts all of this on teardown — offline, self-contained).
    monkeypatch.delitem(sys.modules, "screencap._provisioned", raising=False)
    monkeypatch.delattr(screencap, "_provisioned", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_SyntaxErrorFinder(), *sys.meta_path])

    with pytest.raises(SyntaxError):
        a._provisioned_value("FIREBASE_API_KEY")
    with pytest.raises(SyntaxError):
        a._api_key()


def test_non_str_provisioned_value_degrades_to_placeholder(monkeypatch, _no_cred_env):
    # A PRESENT module whose constant is a non-str value must NOT flow that bad type
    # into a network call: _provisioned_value returns None for it → the resolver falls
    # through to the placeholder, which the U2 release guard then catches (fail-closed).
    _install_provisioned(
        monkeypatch,
        _fake_provisioned(FIREBASE_API_KEY=12345, OAUTH_CLIENT_ID=["not", "a", "str"]),
    )
    assert a._provisioned_value("FIREBASE_API_KEY") is None
    assert a._api_key() == a.DEFAULT_FIREBASE_API_KEY
    assert a._oauth_client_id() == a.DEFAULT_OAUTH_CLIENT_ID


def test_empty_string_env_override_falls_through_to_lower_layers(monkeypatch, _no_cred_env):
    # INTENTIONAL contract: an explicitly empty credential env var means "unset" — the
    # `or`-chain falls THROUGH to the _provisioned layer (when present) or the
    # placeholder (when absent), never resolving to "".
    monkeypatch.setenv("SCREENCAP_FIREBASE_API_KEY", "")
    monkeypatch.setenv("SCREENCAP_OAUTH_CLIENT_ID", "")
    _install_provisioned(
        monkeypatch,
        _fake_provisioned(FIREBASE_API_KEY="prov-key", OAUTH_CLIENT_ID="prov-id"),
    )
    # Empty env is treated as unset → falls through to the provisioned value, not "".
    assert a._api_key() == "prov-key"
    assert a._oauth_client_id() == "prov-id"


def test_empty_string_env_override_falls_through_to_placeholder_when_absent(
    monkeypatch, _no_cred_env
):
    # Symmetric to the above with no _provisioned module: empty env → placeholder.
    monkeypatch.setenv("SCREENCAP_FIREBASE_API_KEY", "")
    monkeypatch.setenv("SCREENCAP_OAUTH_CLIENT_ID", "")
    _install_provisioned(monkeypatch, None)
    assert a._api_key() == a.DEFAULT_FIREBASE_API_KEY
    assert a._oauth_client_id() == a.DEFAULT_OAUTH_CLIENT_ID


def test_is_placeholder_credential_keyed_off_constants(monkeypatch, _no_cred_env):
    # The guard's placeholder detector must track the actual DEFAULT_* sentinels.
    assert a.is_placeholder_credential(a.DEFAULT_FIREBASE_API_KEY)
    assert a.is_placeholder_credential(a.DEFAULT_OAUTH_CLIENT_ID)
    assert not a.is_placeholder_credential("AIzaSyRealLookingWebKey")
    assert not a.is_placeholder_credential("123.apps.googleusercontent.com")


def test_bundled_credentials_ignores_env(monkeypatch):
    # bundled_credentials() must reflect ONLY what ships (_provisioned > placeholder),
    # never the build-shell env — otherwise the release guard could be masked.
    monkeypatch.setenv("SCREENCAP_FIREBASE_API_KEY", "env-key")
    monkeypatch.setenv("SCREENCAP_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("SCREENCAP_OAUTH_CLIENT_SECRET", "env-secret")
    _install_provisioned(monkeypatch, None)
    api_key, client_id, client_secret = a.bundled_credentials()
    assert api_key == a.DEFAULT_FIREBASE_API_KEY  # env ignored → placeholder
    assert client_id == a.DEFAULT_OAUTH_CLIENT_ID
    assert client_secret == ""  # env ignored; no placeholder → empty (guard rejects)


def test_oauth_client_secret_resolves_from_provisioned(monkeypatch, _no_cred_env):
    # The Google Desktop-client secret must ship via _provisioned like the client id,
    # NOT depend on an env var no shipped app has. Regression for the sign-in failure
    # "Google token exchange failed: invalid_request: client_secret is missing."
    monkeypatch.delenv("SCREENCAP_OAUTH_CLIENT_SECRET", raising=False)
    _install_provisioned(
        monkeypatch,
        _fake_provisioned(
            FIREBASE_API_KEY="prov-key",
            OAUTH_CLIENT_ID="prov-id",
            OAUTH_CLIENT_SECRET="prov-secret",
        ),
    )
    assert a._oauth_client_secret() == "prov-secret"


def test_oauth_client_secret_tolerates_secretless_provisioned(monkeypatch, _no_cred_env):
    # A present _provisioned MISSING the secret (a stale module, or a secret-less
    # client) must degrade to "" — never crash sign-in with AttributeError. The
    # release guard is the fail-closed net for a mis-provisioned release, not this
    # runtime resolver.
    monkeypatch.delenv("SCREENCAP_OAUTH_CLIENT_SECRET", raising=False)
    _install_provisioned(
        monkeypatch, _fake_provisioned(FIREBASE_API_KEY="prov-key", OAUTH_CLIENT_ID="prov-id")
    )
    assert a._oauth_client_secret() == ""


def test_exchange_sends_client_secret_when_resolved(monkeypatch):
    # When a secret resolves, it MUST be in the token-exchange POST body — otherwise
    # Google rejects a Desktop-client exchange with "client_secret is missing".
    monkeypatch.setattr(a, "_oauth_client_secret", lambda: "shhh")
    monkeypatch.setattr(a, "_oauth_client_id", lambda: "cid")
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"id_token": "gtok"}

    def _fake_post(url, data=None, timeout=None):
        captured.update(data)
        return _Resp()

    monkeypatch.setattr(a.requests, "post", _fake_post)
    assert a._exchange_code_for_google_token("code", "verifier", "http://127.0.0.1:1") == "gtok"
    assert captured["client_secret"] == "shhh"


def test_exchange_omits_client_secret_when_unresolved(monkeypatch):
    # Source checkout / dev: no secret resolves → the key is absent (not an empty
    # string) so a secret-less client config isn't sent a bogus "".
    monkeypatch.setattr(a, "_oauth_client_secret", lambda: "")
    monkeypatch.setattr(a, "_oauth_client_id", lambda: "cid")
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"id_token": "gtok"}

    monkeypatch.setattr(
        a.requests, "post", lambda url, data=None, timeout=None: captured.update(data) or _Resp()
    )
    a._exchange_code_for_google_token("code", "verifier", "http://127.0.0.1:1")
    assert "client_secret" not in captured


def test_env_var_overrides_provisioned_and_placeholder(monkeypatch):
    monkeypatch.setenv("SCREENCAP_FIREBASE_API_KEY", "env-key")
    monkeypatch.setenv("SCREENCAP_OAUTH_CLIENT_ID", "env-id")
    _install_provisioned(
        monkeypatch,
        _fake_provisioned(FIREBASE_API_KEY="prov-key", OAUTH_CLIENT_ID="prov-id"),
    )
    assert a._api_key() == "env-key"
    assert a._oauth_client_id() == "env-id"


# --- the generator (scripts/generate_provisioned.py) ----------------------


def _load_generator() -> types.ModuleType:
    # auth.py lives at <root>/src/screencap/auth.py; the generator at
    # <root>/scripts/generate_provisioned.py. Resolve from auth.py so the test
    # finds the same tree pytest imported (PYTHONPATH=src in a worktree).
    root = Path(a.__file__).resolve().parents[2]
    gen_path = root / "scripts" / "generate_provisioned.py"
    spec = importlib.util.spec_from_file_location("generate_provisioned", gen_path)
    assert spec is not None and spec.loader is not None, f"cannot load {gen_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generator_writes_module_with_env_values(monkeypatch, tmp_path):
    gen = _load_generator()
    out = tmp_path / "_provisioned.py"
    monkeypatch.setattr(gen, "_OUTPUT_PATH", out)
    monkeypatch.setenv("SCREENCAP_OAUTH_CLIENT_ID", "gen-id")
    monkeypatch.setenv("SCREENCAP_FIREBASE_API_KEY", "gen-key")

    assert gen.main() == 0
    assert out.exists()

    ns: dict = {}
    exec(compile(out.read_text(), str(out), "exec"), ns)
    assert ns["OAUTH_CLIENT_ID"] == "gen-id"
    assert ns["FIREBASE_API_KEY"] == "gen-key"


@pytest.mark.parametrize(
    "to_set",
    [
        (),  # both missing
        ("SCREENCAP_OAUTH_CLIENT_ID",),  # api key missing
        ("SCREENCAP_FIREBASE_API_KEY",),  # client id missing
    ],
)
def test_generator_exits_nonzero_and_writes_nothing_when_var_missing(
    monkeypatch, tmp_path, to_set
):
    gen = _load_generator()
    out = tmp_path / "_provisioned.py"
    monkeypatch.setattr(gen, "_OUTPUT_PATH", out)
    monkeypatch.delenv("SCREENCAP_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SCREENCAP_FIREBASE_API_KEY", raising=False)
    for name in to_set:
        monkeypatch.setenv(name, "x")

    assert gen.main() == 1
    assert not out.exists()  # fail-closed: nothing written


def test_generator_treats_whitespace_only_values_as_missing(monkeypatch, tmp_path):
    # `.strip()` makes whitespace-only env vars count as missing → fail-closed.
    gen = _load_generator()
    out = tmp_path / "_provisioned.py"
    monkeypatch.setattr(gen, "_OUTPUT_PATH", out)
    monkeypatch.setenv("SCREENCAP_OAUTH_CLIENT_ID", "   ")
    monkeypatch.setenv("SCREENCAP_FIREBASE_API_KEY", "\t\n")

    assert gen.main() == 1
    assert not out.exists()


def test_generator_unlinks_stale_module_on_failure(monkeypatch, tmp_path):
    # A fail-closed run must not leave a prior build's module behind to be re-bundled.
    gen = _load_generator()
    out = tmp_path / "_provisioned.py"
    out.write_text("FIREBASE_API_KEY = 'stale'\nOAUTH_CLIENT_ID = 'stale'\n")
    monkeypatch.setattr(gen, "_OUTPUT_PATH", out)
    monkeypatch.delenv("SCREENCAP_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SCREENCAP_FIREBASE_API_KEY", raising=False)

    assert gen.main() == 1
    assert not out.exists()  # stale module removed, not left for the next build


# --------------------------------------------------------------------------
# SCR-241 — access-group storage + legacy fallback + migration (U2/U3)
# --------------------------------------------------------------------------


def test_entitled_store_and_load_use_group_not_keyring(fake_group):
    a._store_refresh_token("tok-abc")
    assert a._load_refresh_token() == "tok-abc"
    assert fake_group.group[_GROUP_KEY] == "tok-abc"
    assert fake_group.keyring_calls == []  # keyring untouched on the entitled path


def test_unentitled_store_and_load_fall_back_to_keyring(fake_keyring):
    a._store_refresh_token("tok-fallback")
    assert fake_keyring[_KEY] == "tok-fallback"  # landed in the legacy keyring
    assert a._load_refresh_token() == "tok-fallback"


def test_entitled_delete_clears_group_and_also_legacy_keyring(fake_group):
    a._store_refresh_token("tok")
    a._delete_refresh_token()
    assert a._load_refresh_token() is None
    # belt-and-braces: the legacy keyring item is cleared too, so it can't
    # resurrect via a later migration read.
    assert ("del", *_KEY) in fake_group.keyring_calls


def test_entitled_store_keychainerror_becomes_autherror_not_raw_runtimeerror(fake_group, monkeypatch):
    # A non-MissingEntitlement group error must surface as AuthError (the CLI's
    # vocabulary), NOT a raw KeychainError — else `screencap login` crashes on it.
    from screencap import keychain_group

    def _boom(*_a, **_k):
        raise keychain_group.KeychainError(-25291, "SecItemAdd")  # errSecNotAvailable

    monkeypatch.setattr(keychain_group, "store", _boom)
    with pytest.raises(a.AuthError):
        a._store_refresh_token("x")
    # and it does NOT fall back to keyring (a real error is not "un-entitled")
    assert fake_group.keyring_calls == []


def test_entitled_load_keychainerror_becomes_autherror_not_raw_runtimeerror(fake_group, monkeypatch):
    # Same for the load path — else `screencap upload`'s get_id_token pre-flight
    # crashes instead of degrading.
    from screencap import keychain_group

    def _boom(*_a, **_k):
        raise keychain_group.KeychainError(-25291, "SecItemCopyMatching")

    monkeypatch.setattr(keychain_group, "load", _boom)
    with pytest.raises(a.AuthError):
        a._load_refresh_token()


def test_migration_silent_hit_moves_legacy_into_group(fake_group):
    fake_group.legacy[_KEY] = "legacy-tok"  # group empty, legacy present + readable
    assert a._load_refresh_token() == "legacy-tok"
    assert fake_group.group[_GROUP_KEY] == "legacy-tok"  # migrated into the group
    assert _KEY not in fake_group.legacy  # legacy copy removed


def test_migration_silent_miss_returns_none_no_group_write(fake_group):
    # group empty, legacy not silently readable (load_legacy → None)
    assert a._load_refresh_token() is None
    assert fake_group.group == {}


def test_migration_failed_legacy_delete_warns_but_keeps_token(fake_group, monkeypatch, caplog):
    from screencap import keychain_group

    fake_group.legacy[_KEY] = "legacy-tok"
    monkeypatch.setattr(keychain_group, "delete_legacy_noninteractive", lambda s, ac: False)
    with caplog.at_level("WARNING", logger="screencap.auth"):
        assert a._load_refresh_token() == "legacy-tok"  # still signed in
    assert fake_group.group[_GROUP_KEY] == "legacy-tok"  # migrated despite delete failure
    assert "duplicated" in caplog.text  # duplicate-token window is surfaced, not hidden


def test_backend_log_never_contains_the_secret(fake_group, caplog):
    with caplog.at_level("DEBUG", logger="screencap.auth"):
        a._store_refresh_token("super-secret-token-xyz")
        a._load_refresh_token()
    assert "super-secret-token-xyz" not in caplog.text


def test_get_id_token_surfaces_autherror_on_group_keychainerror(fake_group, monkeypatch):
    # Full path: get_id_token -> _ensure_fresh -> _load_refresh_token. A keychain
    # error must reach callers as AuthError (which upload's pre-flight handles),
    # never a raw KeychainError traceback.
    from screencap import keychain_group

    monkeypatch.setattr(
        keychain_group, "load", lambda *_a: (_ for _ in ()).throw(keychain_group.KeychainError(-25291, "load"))
    )
    with pytest.raises(a.AuthError):
        a.get_id_token()


def test_entitled_delete_keychainerror_still_clears_legacy(fake_group, monkeypatch):
    # A group-delete failure must NOT skip the belt-and-braces legacy cleanup.
    from screencap import keychain_group

    monkeypatch.setattr(
        keychain_group, "delete", lambda *_a: (_ for _ in ()).throw(keychain_group.KeychainError(-25291, "delete"))
    )
    a._delete_refresh_token()  # must not raise (logout must complete)
    assert ("del", *_KEY) in fake_group.keyring_calls  # legacy cleanup ran anyway


def test_frozen_build_fallback_warns_unfrozen_stays_debug(fake_keyring, monkeypatch, caplog):
    # The silent-degradation alarm: an entitled/frozen build that falls back to
    # keyring WARNs; an un-frozen (CLI/dev) context stays at debug. fake_keyring
    # forces the MissingEntitlement fallback; pin platform so the group branch runs.
    monkeypatch.setattr(a.sys, "platform", "darwin")
    monkeypatch.setattr(a.sys, "frozen", True, raising=False)
    with caplog.at_level("DEBUG", logger="screencap.auth"):
        a._store_refresh_token("tok")
    assert any(r.levelname == "WARNING" and "fell back" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(a.sys, "frozen", False, raising=False)
    with caplog.at_level("DEBUG", logger="screencap.auth"):
        a._store_refresh_token("tok")
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_migration_group_write_failure_keeps_legacy_and_token(fake_group, monkeypatch):
    # Migration read succeeds but the group WRITE fails: keep the user signed in
    # on the legacy token, do NOT delete the legacy copy (distinct from a failed
    # *delete*).
    from screencap import keychain_group

    fake_group.legacy[_KEY] = "legacy-tok"
    monkeypatch.setattr(
        keychain_group, "store", lambda *_a: (_ for _ in ()).throw(keychain_group.KeychainError(-25291, "store"))
    )
    deleted = []
    monkeypatch.setattr(keychain_group, "delete_legacy_noninteractive", lambda s, ac: deleted.append((s, ac)) or True)
    assert a._load_refresh_token() == "legacy-tok"  # still signed in on the legacy token
    assert fake_group.group == {}  # nothing written to the group
    assert deleted == []  # legacy copy NOT deleted after a failed write


def test_entitlement_group_matches_auth_constant():
    # The .entitlements literal and KEYCHAIN_ACCESS_GROUP MUST stay identical, or
    # an entitled build silently falls back to keyring (KTD-3 drift guard).
    import os
    import plistlib
    from pathlib import Path

    if os.environ.get("SCREENCAP_KEYCHAIN_ACCESS_GROUP"):
        pytest.skip("access group overridden via env; drift guard checks the shipped default")
    root = Path(__file__).resolve().parents[1]
    ent = plistlib.loads((root / "macos/ScreenCap/Scripts/screencap-cli.entitlements").read_bytes())
    groups = ent.get("keychain-access-groups", [])
    assert a.KEYCHAIN_ACCESS_GROUP in groups, (
        f"auth.KEYCHAIN_ACCESS_GROUP={a.KEYCHAIN_ACCESS_GROUP!r} not in entitlement {groups!r}"
    )
