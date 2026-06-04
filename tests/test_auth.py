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
