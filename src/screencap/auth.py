"""Client-side Firebase auth: ``login`` / ``logout`` / ``whoami`` + ``get_id_token``.

The macOS app + CLI are the ONLY auth surface (the website has no login). Sign-in
is a system-browser **loopback (RFC 8252) + PKCE** flow to Google, exchanged via
Firebase's ``signInWithIdp`` into a Firebase **ID token** (~1h) and **refresh
token** (long-lived). The refresh token is the durable credential and lives in
the macOS Keychain; the ID token is cached in memory and refreshed transparently
a few minutes before expiry.

Security posture (see SECURITY.md + docs/runbooks/cloud-auth-setup.md):

* The Keychain entry uses the **default "Always Allow" trusted-binary ACL** (the
  same posture as ``network/crypto.py``) — NOT a code-signing-pinned ACL. Any
  same-user trusted binary can read it. Documented honestly; we do not claim
  pinning that does not exist.
* The OAuth client is a **public native client**: no ``client_secret`` is
  embedded in source or binaries. PKCE is the protection (RFC 9700). An env
  override (``SCREENCAP_OAUTH_CLIENT_SECRET``) exists only as an escape hatch for
  a client config that insists on a secret — nothing is shipped.

Config knobs mirror ``SCREENCAP_UPLOAD_URL`` (env-overridable, provisioned
defaults): ``SCREENCAP_FIREBASE_API_KEY`` and ``SCREENCAP_OAUTH_CLIENT_ID``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

# --------------------------------------------------------------------------
# Config (env-overridable; provisioned values from docs/runbooks/cloud-auth-setup.md)
# --------------------------------------------------------------------------

# These ship in the client and are NOT secrets (public native OAuth client + a
# browser API key restricted to Identity Toolkit + Token Service). They are
# placeholders until U1 provisioning fills them in; the env overrides let dev and
# tests point at a real project without a rebuild.
DEFAULT_FIREBASE_API_KEY = "REPLACE_WITH_PROVISIONED_WEB_API_KEY"
DEFAULT_OAUTH_CLIENT_ID = "REPLACE_WITH_PROVISIONED_DESKTOP_CLIENT_ID.apps.googleusercontent.com"

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SIGN_IN_WITH_IDP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp"
SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"

# Keychain coordinates. Stable across versions — changing them orphans the stored
# refresh token (the user just signs in again, so this is recoverable, unlike the
# network KEK).
KEYCHAIN_SERVICE = "screencap-auth"
KEYCHAIN_ACCOUNT = "default"

# Refresh the ID token when it is within this window of expiry, so a call never
# races the ~1h boundary.
_REFRESH_BUFFER_SECONDS = 300
# How long `screencap login` waits for the browser round-trip before giving up,
# so the app's "waiting for sign-in" UI cannot hang forever (U6 depends on this).
LOGIN_TIMEOUT_SECONDS = 180


def _api_key() -> str:
    return os.environ.get("SCREENCAP_FIREBASE_API_KEY", DEFAULT_FIREBASE_API_KEY)


def _oauth_client_id() -> str:
    return os.environ.get("SCREENCAP_OAUTH_CLIENT_ID", DEFAULT_OAUTH_CLIENT_ID)


def _oauth_client_secret() -> str:
    # Empty by default — public native client, no embedded secret.
    return os.environ.get("SCREENCAP_OAUTH_CLIENT_SECRET", "")


# --------------------------------------------------------------------------
# Errors + state
# --------------------------------------------------------------------------


class AuthError(Exception):
    """A login or refresh failure that is NOT simply 'not signed in' — e.g. a
    network error, a state mismatch, or an identity-provider rejection. Surfaced
    loudly; never swallowed."""


class NotSignedIn(AuthError):
    """No usable stored credential — the user must run ``screencap login``.

    Distinct from a transient AuthError so the cloud paths (U5) can tell "sign in
    first" apart from "try again later". A definitively bad/expired/revoked
    refresh token resolves here.
    """


@dataclass
class AuthState:
    id_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    uid: str
    email: str | None = None


# In-memory cache of the current session. The ID token is NEVER persisted; only
# the refresh token lives in the Keychain.
_cached: AuthState | None = None
_thread_lock = threading.Lock()


# --------------------------------------------------------------------------
# Keychain wrappers (lazy keyring import, mirroring network/crypto.py)
# --------------------------------------------------------------------------


def _store_refresh_token(token: str) -> None:
    import keyring

    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, token)


def _load_refresh_token() -> str | None:
    import keyring

    return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)


def _delete_refresh_token() -> None:
    import keyring

    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass  # nothing stored — already signed out


def _lock_path():
    from screencap.config import get_base_dir

    run_dir = get_base_dir() / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "auth-refresh.lock"


@contextmanager
def _refresh_lock():
    """Serialize the read-refresh-store critical section across threads AND
    processes, so two concurrent ``screencap`` invocations cannot each rotate the
    refresh token and silently drop one rotation."""
    with _thread_lock:
        f = None
        try:
            try:
                f = open(_lock_path(), "w")
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX)
            except (OSError, ImportError):
                f = None  # best-effort cross-process lock; thread lock still held
            yield
        finally:
            if f is not None:
                try:
                    import fcntl

                    fcntl.flock(f, fcntl.LOCK_UN)
                except Exception:
                    pass
                f.close()


# --------------------------------------------------------------------------
# PKCE + JWT helpers
# --------------------------------------------------------------------------


def _gen_code_verifier() -> str:
    # 43–128 chars of unreserved characters per RFC 7636.
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _decode_id_token_claims(id_token: str) -> dict:
    """Decode (WITHOUT verifying) the JWT payload for client-side display only.

    The server is the verifier of record; here we only need email/uid to show in
    ``whoami`` and to label the session. Returns {} if the token is malformed.
    """
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


# --------------------------------------------------------------------------
# Loopback OAuth (RFC 8252)
# --------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        params = parse_qs(urlparse(self.path).query)
        self.server.callback_code = params.get("code", [None])[0]
        self.server.callback_state = params.get("state", [None])[0]
        self.server.callback_error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif'>"
            b"<h2>Sign-in complete.</h2>"
            b"<p>You can close this tab and return to ScreenCap.</p>"
            b"</body></html>"
        )

    def log_message(self, *args):  # silence default stderr logging
        pass


def _authorize_url(redirect_uri: str, challenge: str, state: str) -> str:
    params = {
        "client_id": _oauth_client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def _validate_callback(code, state, error, expected_state) -> str:
    """Validate the loopback callback. Raises AuthError on any anomaly; returns
    the authorization code on success. Extracted so the CSRF/state check is
    unit-testable without a real socket."""
    if error:
        raise AuthError(f"Authorization was declined or failed: {error}")
    if not code:
        raise AuthError("Sign-in did not complete (timed out or cancelled).")
    if not expected_state or state != expected_state:
        raise AuthError("State mismatch on the sign-in callback (possible CSRF).")
    return code


def _login_via_browser(timeout: float, open_browser: bool) -> tuple[str, str, str]:
    """Run the loopback + PKCE browser flow. Returns (code, redirect_uri, verifier)."""
    import webbrowser

    verifier = _gen_code_verifier()
    challenge = _code_challenge(verifier)
    state = secrets.token_urlsafe(24)

    # Bind an ephemeral loopback port FIRST so the redirect_uri reflects the
    # actual 127.0.0.1:<port> chosen at runtime (a static value would weaken the
    # loopback/port CSRF protection — see the plan's implementation note).
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.callback_code = server.callback_state = server.callback_error = None
    try:
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}"
        authorize_url = _authorize_url(redirect_uri, challenge, state)
        if open_browser:
            webbrowser.open(authorize_url)
        server.timeout = timeout
        server.handle_request()  # blocks until one callback or `timeout` elapses
        code = _validate_callback(
            server.callback_code, server.callback_state, server.callback_error, state
        )
        return code, redirect_uri, verifier
    finally:
        server.server_close()


def _exchange_code_for_google_token(code: str, verifier: str, redirect_uri: str) -> str:
    data = {
        "code": code,
        "client_id": _oauth_client_id(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    secret = _oauth_client_secret()
    if secret:
        data["client_secret"] = secret
    try:
        resp = requests.post(GOOGLE_TOKEN_ENDPOINT, data=data, timeout=30)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise AuthError(f"Could not reach Google token endpoint: {e}")
    if resp.status_code != 200:
        raise AuthError(f"Google token exchange failed: {_describe_error(resp)}")
    id_token = resp.json().get("id_token")
    if not id_token:
        raise AuthError("Google token exchange returned no id_token.")
    return id_token


def _sign_in_with_idp(google_id_token: str, redirect_uri: str) -> AuthState:
    try:
        resp = requests.post(
            SIGN_IN_WITH_IDP_URL,
            params={"key": _api_key()},
            json={
                "postBody": f"id_token={google_id_token}&providerId=google.com",
                "requestUri": redirect_uri,
                "returnSecureToken": True,
                "returnIdpCredential": False,
            },
            timeout=30,
        )
    except (requests.ConnectionError, requests.Timeout) as e:
        raise AuthError(f"Could not reach the identity provider: {e}")
    if resp.status_code != 200:
        raise AuthError(f"Sign-in failed: {_describe_error(resp)}")
    d = resp.json()
    return AuthState(
        id_token=d["idToken"],
        refresh_token=d["refreshToken"],
        expires_at=time.time() + int(d.get("expiresIn", 3600)),
        uid=d.get("localId", ""),
        email=d.get("email"),
    )


def _describe_error(resp) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            return err.get("message", resp.text)
        return str(err or resp.text)
    except Exception:
        return resp.text


# --------------------------------------------------------------------------
# Token refresh
# --------------------------------------------------------------------------


def _refresh(refresh_token: str) -> AuthState:
    """Exchange a refresh token for a fresh ID token via the secure-token API.

    A transient network failure raises AuthError (retryable). A non-200 means the
    stored refresh token is bad/expired/revoked -> NotSignedIn (the user must
    sign in again). Any rotated refresh token is persisted.
    """
    try:
        resp = requests.post(
            SECURE_TOKEN_URL,
            params={"key": _api_key()},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=30,
        )
    except (requests.ConnectionError, requests.Timeout) as e:
        raise AuthError(f"Token refresh failed (network): {e}")
    if resp.status_code != 200:
        raise NotSignedIn("Your session has expired. Run `screencap login` again.")
    d = resp.json()
    new_refresh = d.get("refresh_token", refresh_token)
    id_token = d["id_token"]
    claims = _decode_id_token_claims(id_token)
    state = AuthState(
        id_token=id_token,
        refresh_token=new_refresh,
        expires_at=time.time() + int(d.get("expires_in", 3600)),
        uid=claims.get("user_id") or claims.get("sub") or d.get("user_id", ""),
        email=claims.get("email"),
    )
    if new_refresh != refresh_token:
        _store_refresh_token(new_refresh)  # persist rotation
    return state


def _ensure_fresh() -> AuthState:
    """Return a valid AuthState, refreshing if the cached ID token is near expiry.

    Raises NotSignedIn if there is no stored refresh token.
    """
    global _cached
    now = time.time()
    if _cached and _cached.expires_at - now > _REFRESH_BUFFER_SECONDS:
        return _cached

    with _refresh_lock():
        # Re-check inside the lock: another thread/process may have just
        # refreshed (and rotated) while we waited.
        now = time.time()
        if _cached and _cached.expires_at - now > _REFRESH_BUFFER_SECONDS:
            return _cached
        refresh_token = (_cached.refresh_token if _cached else None) or _load_refresh_token()
        if not refresh_token:
            raise NotSignedIn("Not signed in. Run `screencap login` to upload to the cloud.")
        _cached = _refresh(refresh_token)
        return _cached


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def get_id_token() -> str:
    """Return a valid Firebase ID token, refreshing transparently if needed.

    Raises NotSignedIn if there is no stored credential, or AuthError on a
    transient refresh failure (network). Callers attach the result as
    ``Authorization: Bearer <token>``.
    """
    return _ensure_fresh().id_token


def login(open_browser: bool = True, timeout: float = LOGIN_TIMEOUT_SECONDS) -> AuthState:
    """Run the interactive sign-in flow and persist the refresh token.

    Opens the system browser to Google, captures the loopback callback, exchanges
    the code (PKCE) for a Google id_token, signs in to Firebase via signInWithIdp,
    and stores the refresh token in the Keychain. Returns the resulting AuthState.
    """
    global _cached
    code, redirect_uri, verifier = _login_via_browser(timeout, open_browser)
    google_id_token = _exchange_code_for_google_token(code, verifier, redirect_uri)
    state = _sign_in_with_idp(google_id_token, redirect_uri)
    with _refresh_lock():
        _store_refresh_token(state.refresh_token)
    _cached = state
    return state


def logout() -> bool:
    """Clear the in-memory token and delete the stored refresh token.

    Returns True if a credential was present (or cleared), False if nothing was
    stored. Never raises for the common cases.
    """
    global _cached
    _cached = None
    had_token = False
    try:
        had_token = _load_refresh_token() is not None
    except Exception:
        pass
    try:
        _delete_refresh_token()
    except Exception:
        return False
    return had_token


def whoami() -> dict:
    """Report sign-in state without forcing a refresh failure to crash.

    Returns {"signed_in": False} when no credential is stored. When signed in,
    returns uid/email; if a refresh is needed but fails (e.g. offline), reports
    signed_in=True with stale=True rather than raising.
    """
    try:
        refresh_token = _load_refresh_token()
    except Exception:
        refresh_token = None
    if not refresh_token:
        return {"signed_in": False}
    try:
        state = _ensure_fresh()
        return {"signed_in": True, "uid": state.uid, "email": state.email}
    except NotSignedIn:
        return {"signed_in": False}
    except AuthError:
        # We have a refresh token but couldn't refresh right now (e.g. offline).
        return {"signed_in": True, "uid": None, "email": None, "stale": True}
