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
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, urlencode, urlparse

import requests

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Config (env-overridable; provisioned values from docs/runbooks/cloud-auth-setup.md)
# --------------------------------------------------------------------------

# These ship in the client and are NOT secrets (public native OAuth client + a
# browser API key restricted to Identity Toolkit + Token Service). The source tree
# carries only these placeholders — the real provisioned values are injected at
# build time into a gitignored ``screencap._provisioned`` module (written by
# ``scripts/generate_provisioned.py``) which the resolvers below consult. Precedence:
#   explicit env var (SCREENCAP_*)  >  screencap._provisioned  >  placeholder
# so dev/.env and tests keep overriding without a rebuild, release binaries get the
# injected values, and an unconfigured build resolves to the placeholder (the U2
# build guard rejects that for release/tag builds). "Inject, don't commit" keeps the
# values out of git; it is not a security boundary (they ship in the binary anyway).
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

# Out-of-band engine token channel. The live-upload ``ChunkProcessor`` runs inside
# the daemon-spawned engine subprocess, whose Keychain ACL identity differs from the
# interactive ``login`` binary — so the engine must NOT read keyring. Instead the
# daemon reads the ID token in its own ACL context and writes a short-lived token to
# a 0600 file, passing the file *path* (not the secret) to the engine via this env
# var. The token in the file is same-EUID-protected, consistent with the daemon
# socket trust boundary (see SECURITY.md). The daemon re-mints + rewrites the file on
# a timer for recordings that outlast the ~1h ID token, so the engine reads it fresh
# on every cloud call. Never delivered via ``_worker_args``/argv (base64'd into the
# command line, ``ps``-visible).
ENGINE_TOKEN_FILE_ENV = "SCREENCAP_ENGINE_TOKEN_FILE"

# Refresh the ID token when it is within this window of expiry, so a call never
# races the ~1h boundary.
_REFRESH_BUFFER_SECONDS = 300
# How long `screencap login` waits for the browser round-trip before giving up,
# so the app's "waiting for sign-in" UI cannot hang forever (U6 depends on this).
LOGIN_TIMEOUT_SECONDS = 180


def _provisioned_value(attr: str) -> str | None:
    """Return a build-time-injected credential, or ``None`` if not injected.

    Reads ``attr`` from the gitignored ``screencap._provisioned`` module written by
    ``scripts/generate_provisioned.py`` at build time. Returns ``None`` when the
    module is ABSENT (source checkout / test suite / unconfigured build) so the
    caller falls back to the placeholder.

    The ``ImportError`` catch is deliberately narrow. An absent module
    (``ModuleNotFoundError``, an ``ImportError`` subclass) is the normal
    not-injected case → ``None`` → placeholder. The fail-loud / fail-closed cases:

    * a module that is PRESENT but syntactically invalid (``SyntaxError``, not an
      ``ImportError`` subclass, not caught here) → propagates loudly;
    * a module that is PRESENT but missing the expected constant (``getattr`` with no
      default → ``AttributeError``) → propagates loudly;
    * a module that is PRESENT with the constant set to a non-``str`` value → ``None``
      → placeholder, which the U2 release guard then catches (fail-closed at release).
    """
    try:
        from screencap import _provisioned
    except ImportError:
        return None
    # getattr WITHOUT a default: a missing constant must still raise AttributeError
    # (loud), never degrade to the placeholder. The isinstance check applies only to
    # a PRESENT value — a non-str constant degrades to None → placeholder → caught by
    # the release guard, rather than flowing a bad type into a network call.
    value = getattr(_provisioned, attr)
    return value if isinstance(value, str) else None


def _api_key() -> str:
    # Precedence: explicit env var > injected _provisioned value > placeholder.
    # An empty-string env override (SCREENCAP_FIREBASE_API_KEY="") is treated as
    # unset by design — the `or`-chain falls through to the lower layers.
    return (
        os.environ.get("SCREENCAP_FIREBASE_API_KEY")
        or _provisioned_value("FIREBASE_API_KEY")
        or DEFAULT_FIREBASE_API_KEY
    )


def _oauth_client_id() -> str:
    # Precedence: explicit env var > injected _provisioned value > placeholder.
    # An empty-string env override (SCREENCAP_OAUTH_CLIENT_ID="") is treated as
    # unset by design — the `or`-chain falls through to the lower layers.
    return (
        os.environ.get("SCREENCAP_OAUTH_CLIENT_ID")
        or _provisioned_value("OAUTH_CLIENT_ID")
        or DEFAULT_OAUTH_CLIENT_ID
    )


def is_placeholder_credential(value: str) -> bool:
    """True if a resolved credential is still an un-provisioned source placeholder.

    Keyed off the actual ``DEFAULT_*`` sentinels (single source of truth) so the
    U2 release guard can't silently drift if the placeholder strings ever change.
    """
    return value in (DEFAULT_FIREBASE_API_KEY, DEFAULT_OAUTH_CLIENT_ID)


def bundled_credentials() -> tuple[str, str]:
    """Resolve creds as the SHIPPED binary will for an end user — ``_provisioned``
    (injected at build time) then the placeholder, **ignoring env vars**.

    The U2 release guard uses this rather than :func:`_api_key`/:func:`_oauth_client_id`
    so a build-shell env var or a stray ``.env`` (which ``load_dotenv`` reads at
    startup) cannot mask a ``_provisioned`` bundling failure: an end user has neither,
    so the guard must prove what is actually bundled. Returns ``(api_key, client_id)``.
    """
    return (
        _provisioned_value("FIREBASE_API_KEY") or DEFAULT_FIREBASE_API_KEY,
        _provisioned_value("OAUTH_CLIENT_ID") or DEFAULT_OAUTH_CLIENT_ID,
    )


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


class WhoAmI(TypedDict, total=False):
    """The shape ``whoami()`` returns and the CLI wraps in the
    ``_AUTH_SCHEMA_VERSION`` envelope read by the SwiftUI shell. A versioned
    cross-layer contract, so it is machine-checkable here (``total=False`` —
    the signed-out case carries only ``signed_in``)."""

    signed_in: bool
    uid: str | None
    email: str | None
    stale: bool


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


def _read_engine_token() -> str | None:
    """Return the daemon-supplied engine ID token, or ``None`` if not in the engine.

    Reads the 0600 token file pointed at by :data:`ENGINE_TOKEN_FILE_ENV`. Returns
    ``None`` when the env var is unset (the normal interactive/login context, which
    falls through to the Keychain path). Returns ``None`` when the env var IS set but
    the file is missing/unreadable/empty — the caller treats that as "no token" and
    fails closed, so a vanished daemon token never silently advances the live-upload
    deletion gates. Reads fresh on every call so a daemon re-mint is picked up
    transparently.
    """
    path = os.environ.get(ENGINE_TOKEN_FILE_ENV)
    if not path:
        return None
    try:
        token = Path(path).read_text().strip()
    except OSError:
        return None
    return token or None


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


def id_token_uid(id_token: str) -> str | None:
    """Best-effort Firebase uid from an (UNVERIFIED) ID token, for client gating.

    Mirrors the uid extraction in :func:`_refresh` (``user_id`` then ``sub``).
    The token is NOT verified here — the server remains the verifier of record —
    so this is only ever used to *refuse* a cross-account action (fail closed,
    SCR-116), never to authorize one. Returns ``None`` for an empty/malformed
    token or one carrying no uid claim, which callers treat as "cannot confirm
    ownership" and fall back to their existing behavior.
    """
    if not id_token:
        return None
    claims = _decode_id_token_claims(id_token)
    return claims.get("user_id") or claims.get("sub") or None


# --------------------------------------------------------------------------
# Loopback OAuth (RFC 8252)
# --------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        params = parse_qs(urlparse(self.path).query)
        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]
        # Only a real OAuth redirect carries `code` or `error`. A browser may hit
        # 127.0.0.1:<port> first with a non-OAuth GET (favicon, connection
        # pre-warm, a speculative prefetch); such a probe must NOT record a result
        # or it would consume the wait and fail the genuine callback. Answer it
        # with a 404 and record nothing — the server keeps waiting.
        if code is None and error is None:
            self.send_response(404)
            self.end_headers()
            return
        self.server.callback_code = code
        self.server.callback_state = params.get("state", [None])[0]
        self.server.callback_error = error
        self.server.callback_received = True
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
    server.callback_received = False
    try:
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}"
        authorize_url = _authorize_url(redirect_uri, challenge, state)
        if open_browser:
            webbrowser.open(authorize_url)
        # Service requests until one actually carries the OAuth callback (or the
        # deadline passes). handle_request() honors server.timeout per call, so a
        # non-OAuth probe (favicon/preconnect) that gets a 404 doesn't end the
        # wait — we loop until callback_received or the overall deadline.
        per_call_timeout = min(timeout, 5.0)
        deadline = time.monotonic() + timeout
        while not server.callback_received and time.monotonic() < deadline:
            server.timeout = per_call_timeout
            server.handle_request()  # returns on a request OR after per_call_timeout
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


def _describe_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error")
        if isinstance(err, dict):
            # Firebase Identity Toolkit: {"error": {"code", "message", ...}}.
            return err.get("message", resp.text)
        # Google OAuth token endpoint: {"error": "invalid_request",
        # "error_description": "client_secret is missing."}. Keep the description —
        # it carries the actionable reason; the bare code alone is undiagnosable.
        desc = body.get("error_description")
        if err and desc:
            return f"{err}: {desc}"
        return str(err or desc or resp.text)
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
    if resp.status_code in (400, 401):
        # The refresh token itself is bad/expired/revoked -> the user must sign in
        # again. Only these statuses mean "dead credential".
        raise NotSignedIn("Your session has expired. Run `screencap login` again.")
    if resp.status_code != 200:
        # 429 / 5xx are transient (rate limit, secure-token outage); the refresh
        # token is still valid. Surface a retryable AuthError, NOT a forced
        # re-login (matters for the U5 fail-closed live-upload state machine).
        raise AuthError(f"Token refresh temporarily failed (HTTP {resp.status_code}).")
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


def _ensure_fresh(force: bool = False) -> AuthState:
    """Return a valid AuthState, refreshing if the cached ID token is near expiry.

    ``force=True`` re-mints from the stored refresh token even when the cached ID
    token is still fresh — used by the cloud paths' 401-retry, where the server
    rejected a token we still believe is valid (clock skew, rotation, revocation).

    Raises NotSignedIn if there is no stored refresh token.
    """
    global _cached
    now = time.time()
    if not force and _cached and _cached.expires_at - now > _REFRESH_BUFFER_SECONDS:
        return _cached

    with _refresh_lock():
        # Re-check inside the lock: another thread/process may have just
        # refreshed (and rotated) while we waited.
        now = time.time()
        if not force and _cached and _cached.expires_at - now > _REFRESH_BUFFER_SECONDS:
            return _cached
        # The Keychain is the rotation source of truth. A long-lived process can
        # hold a STALE in-memory refresh token after another process rotated it;
        # sending the stale one would get rejected and force a needless logout
        # while a valid token sits in the Keychain. So reload and prefer it.
        refresh_token = _load_refresh_token() or (_cached.refresh_token if _cached else None)
        if not refresh_token:
            raise NotSignedIn("Not signed in. Run `screencap login` to upload to the cloud.")
        try:
            _cached = _refresh(refresh_token)
        except NotSignedIn:
            # Another process may have rotated the token during our refresh.
            # Reload once; retry only if it actually changed, before forcing a
            # logout on what could be a freshly-rotated (valid) token.
            latest = _load_refresh_token()
            if latest and latest != refresh_token:
                _cached = _refresh(latest)
            else:
                raise
        return _cached


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def get_id_token(force_refresh: bool = False) -> str:
    """Return a valid Firebase ID token, refreshing transparently if needed.

    The single token entry point for every cloud call. In the daemon-spawned engine
    subprocess (``ENGINE_TOKEN_FILE_ENV`` set) it returns ONLY the daemon-supplied
    out-of-band token — never reading keyring (wrong ACL identity), never falling back
    to the Keychain path — and raises NotSignedIn if the daemon supplied none, so the
    live-upload path fails closed. In the interactive/login context it reads the
    Keychain refresh token. ``force_refresh=True`` re-mints from the refresh token even
    when the cached ID token still looks fresh (used by the cloud paths' 401-retry); it
    has NO effect in the engine context (only the daemon can re-mint — the engine just
    re-reads the file, picking up a daemon re-mint if one has landed).

    Raises NotSignedIn if there is no usable credential, or AuthError on a transient
    refresh failure (network). Callers attach the result as ``Authorization: Bearer``.
    """
    if os.environ.get(ENGINE_TOKEN_FILE_ENV):
        # Engine context: the daemon-supplied file is the ONLY valid source. A
        # missing/empty file → NotSignedIn → the live-upload chunk fails closed
        # (ChunkStatus.FAILED), never advancing the sentinel/stub/delete gates.
        token = _read_engine_token()
        if not token:
            raise NotSignedIn(
                "No engine auth token available (the daemon supplied none)."
            )
        return token
    return _ensure_fresh(force=force_refresh).id_token


def authed_post(
    post: Callable[..., requests.Response],
    url: str,
    *,
    json: object = None,
    timeout: float = 30,
) -> requests.Response:
    """POST ``url`` with an ``Authorization: Bearer`` header, retrying once on 401.

    ``post`` is the CALLER's own ``requests.post`` reference (e.g.
    ``screencap.upload.requests.post``) — passing it in keeps the network call
    in the caller's module so existing test mocks on that attribute keep working,
    while the bearer/refresh/retry policy lives in one place for every cloud path.

    On a 401 (the server rejecting a token we still believe is valid: clock skew,
    rotation, revocation) it forces a token refresh via :func:`get_id_token` and
    retries the single call once. Propagates :class:`NotSignedIn` (callers map it
    to a "run ``screencap login``" message) and any exception ``post`` raises
    (``ConnectionError`` / ``Timeout``). Returns the final response.
    """
    token = get_id_token()
    resp = post(url, json=json, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    if resp.status_code == 401:
        # Token rejected (clock skew / rotation / revocation) — force a refresh and
        # retry the single call once. In the engine context get_id_token re-reads
        # the daemon-supplied file (force_refresh is a no-op there); a still-stale
        # file yields another 401 the caller fails closed on.
        token = get_id_token(force_refresh=True)
        resp = post(url, json=json, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    return resp


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
        # _delete_refresh_token already swallows "nothing stored"
        # (PasswordDeleteError); reaching here means deletion FAILED for another
        # reason (locked Keychain, backend error) and the credential may persist.
        # Report had_token regardless — never claim "nothing was stored" when the
        # token is still there.
        logger.warning("Keychain refresh-token deletion failed; the credential may persist")
    return had_token


def whoami() -> WhoAmI:
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
