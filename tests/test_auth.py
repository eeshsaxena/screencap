"""Tests for the client auth module (screencap.auth).

Network (Google token / signInWithIdp / secure-token refresh) and the macOS
Keychain are both mocked, so these run fully offline. The most subtle surface is
``get_id_token`` refresh/rotation, so that is covered most heavily.
"""

import base64
import json
import time

import pytest

import screencap.auth as a


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
    """In-memory stand-in for the macOS Keychain."""
    import keyring
    import keyring.errors

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
    return store


_KEY = (a.KEYCHAIN_SERVICE, a.KEYCHAIN_ACCOUNT)


def _jwt(claims: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


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


# --------------------------------------------------------------------------
# logout + whoami
# --------------------------------------------------------------------------


def test_logout_deletes_and_whoami_reports_signed_out(fake_keyring):
    fake_keyring[_KEY] = "rt"
    assert a.logout() is True
    assert _KEY not in fake_keyring
    assert a.whoami() == {"signed_in": False}


def test_logout_when_not_signed_in_returns_false(fake_keyring):
    assert a.logout() is False


def test_whoami_signed_in(fake_keyring, monkeypatch):
    fake_keyring[_KEY] = "rt"
    monkeypatch.setattr(
        a, "_ensure_fresh",
        lambda: a.AuthState("id", "rt", time.time() + 3600, "uid1", "e@x.com"),
    )
    assert a.whoami() == {"signed_in": True, "uid": "uid1", "email": "e@x.com"}


def test_whoami_offline_reports_stale_not_crash(fake_keyring, monkeypatch):
    fake_keyring[_KEY] = "rt"

    def boom():
        raise a.AuthError("offline")

    monkeypatch.setattr(a, "_ensure_fresh", boom)
    info = a.whoami()
    assert info["signed_in"] is True
    assert info.get("stale") is True


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

    monkeypatch.setattr(a, "whoami", lambda: {"signed_in": True, "uid": "u", "email": "e@x.com"})
    res = runner.invoke(whoami_cmd, ["--json"])
    payload = json.loads(res.output)
    assert payload["signed_in"] is True
    assert payload["email"] == "e@x.com"
