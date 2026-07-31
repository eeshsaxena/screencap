"""Cloud Function: signed GCS URLs for screencap recording upload/download.

The function stays ``--allow-unauthenticated`` at the Cloud Run layer, but the
auth boundary is IN CODE: every real-user action (upload / list / sign-download)
requires a Firebase bearer token (``verify_bearer``) and is scoped to
``users/{uid}/…`` via the single ``resolve_prefix`` key builder. The public
``demo-*`` actions (added in U3) require no token and only ever read ``demo/``.
Do NOT "tighten" the Cloud Run trigger to require IAM auth — that would also
block the public demo reads; the in-code gate is the only boundary, backed by a
CI contract test asserting no tokenless request reaches any ``users/`` code path.

Token-gated actions (Bearer required):
- (default/no action) / ``upload`` — signed PUT URLs under the caller's namespace
- ``list`` — list the caller's own recordings
- ``sign-download`` — signed GET URLs for one of the caller's own recordings

Deploy (project: proteus-photos, region: southamerica-east1):
    # The function runs as a dedicated SA and signs v4 URLs via the IAM
    # signBlob API, which requires roles/iam.serviceAccountTokenCreator as a
    # RESOURCE-LEVEL self-binding on that SA (not a project-level grant):
    SIGNER=screencap-signer@proteus-photos.iam.gserviceaccount.com
    gcloud iam service-accounts add-iam-policy-binding "$SIGNER" \
        --project proteus-photos \
        --member "serviceAccount:$SIGNER" \
        --role roles/iam.serviceAccountTokenCreator
    # The SA also needs roles/storage.objectAdmin on the target recordings bucket.

    # Deploy. ALWAYS set SCREENCAP_BUCKET explicitly to the staging bucket:
    # 'screencap-recordings' is only the code DEFAULT and the eventual cutover
    # name — that bucket DOES NOT EXIST in proteus-photos until the migration's
    # recordings cutover (U7) creates it and copies the data. Setting it early
    # deploys green and then 403s on the first GCS call, which is what took the
    # public gallery and share resolution down for days. Verify before deploying:
    #     gcloud storage ls "gs://$SCREENCAP_BUCKET/demo/" --project proteus-photos
    # ALWAYS set SCREENCAP_PROJECT_ID explicitly too — it is the only thing that
    # pins the trusted Firebase project; there is NO ambient fallback (see
    # _resolve_project_id):
    gcloud functions deploy get-upload-urls \
        --project proteus-photos --gen2 \
        --runtime python312 \
        --trigger-http \
        --allow-unauthenticated \
        --region southamerica-east1 \
        --source scripts/cloud-function/ \
        --entry-point get_upload_urls \
        --service-account "$SIGNER" \
        --set-env-vars SCREENCAP_BUCKET=screencap-recordings-staging,SCREENCAP_PROJECT_ID=proteus-photos
    # The dev function (get-upload-urls-dev) is identical with
    # SCREENCAP_BUCKET=screencap-recordings-dev-staging,SCREENCAP_PROJECT_ID=proteus-photos
    # (the dev function still verifies proteus-photos tokens — set the project
    # explicitly even if the dev function is deployed in another GCP project).

    # DEPLOY (SCR-137 — gate LIFTED 2026-06-16): the old "hold until clients carry
    # tokens" gate is void. SCR-138 (website repoint to demo-*) shipped, and the live
    # prod get-upload-urls still runs OLD code (demo-list -> 400) so the public gallery
    # is broken. Deploying THIS code is the fix. It is safe re: clients: the shipped
    # client carries only REPLACE_WITH_PROVISIONED_* placeholders (no released tag has
    # authed_post), so the now-token-gated upload/list/sign-download break no working
    # flow; demo-* are tokenless. Dev-validate (get-upload-urls-dev) first, THEN deploy
    # to prod. Separately: the website shows *videos* only once demo/ has content —
    # that is SCR-139 (demo promotion), NOT this deploy; gs://<bucket>/demo/ is empty
    # today, so a fresh deploy yields the empty-gallery placeholder until SCR-139 runs.
    # Full procedure + verification + rollback: docs/runbooks/cloud-auth-setup.md.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import firebase_admin
import functions_framework
import google.api_core.exceptions
import google.auth
import google.auth.exceptions
import google.auth.transport.requests
import shares
from auth import AuthInvalid, AuthUnavailable, verify_bearer, verify_bearer_full
from flask import jsonify
from google.cloud import storage
from paths import PrefixResolutionError, is_valid_name, resolve_prefix

logger = logging.getLogger(__name__)

BUCKET = os.environ.get("SCREENCAP_BUCKET", "screencap-recordings")
UPLOAD_EXPIRY_MINUTES = 15
# Per-namespace GET expiry. users/ data is private: a signed GET URL is an
# UNREVOKABLE bearer capability for its whole lifetime, so it is kept short (the
# minimum the download flow needs). demo/ is public anyway, so a longer window
# is acceptable there (used by the demo handlers in U3).
USER_DOWNLOAD_EXPIRY_MINUTES = 30
DEMO_DOWNLOAD_EXPIRY_HOURS = 4
# Share GET URLs are kept short: revoke/expiry stop FUTURE signing, but an
# already-issued signed URL stays valid for its own TTL (share plan R4/A2), so a
# short window bounds how long a just-revoked link keeps working.
SHARE_DOWNLOAD_EXPIRY_HOURS = 1
MAX_FILES = 500

def _resolve_project_id() -> str:
    """The Firebase project the function trusts.

    ``SCREENCAP_PROJECT_ID`` is the ONLY override. We deliberately do NOT fall
    back to the ambient ``GOOGLE_CLOUD_PROJECT`` (the Cloud Run *hosting* project)
    — an off-default deploy that forgets the override would otherwise silently
    pin the hosting project, and ``verify_bearer`` would then reject every
    legitimate proteus-photos token (fail-closed lockout). Set
    ``SCREENCAP_PROJECT_ID`` explicitly on every deploy.
    """
    return os.environ.get("SCREENCAP_PROJECT_ID", "proteus-photos")


# Firebase project the signing function trusts. PINNED explicitly so the
# function can never verify-but-misattribute a token from a foreign Firebase
# project: verify_bearer (auth.py) re-asserts the decoded token's aud/iss
# against this value, over and above the SDK's own check.
PROJECT_ID = _resolve_project_id()

# File-name guard for per-file upload validation. Slashes allowed (subdirs like
# screenshots/0.png). Recording-NAME validation now lives in
# paths.resolve_prefix (the single key builder), so the old _RECORDING_RE is
# retired here.
_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._/-]{0,511}$")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST",
    "Access-Control-Allow-Headers": "Content-Type",
}

# Module-level client for reuse across warm invocations.
# Cloud Run uses compute engine credentials which can't sign locally —
# we pass service_account_email + access_token so the library uses
# the IAM signBlob API instead. Requires roles/iam.serviceAccountTokenCreator.
_storage_client = storage.Client()
_bucket = _storage_client.bucket(BUCKET)
_credentials, _project = google.auth.default()
_auth_request = google.auth.transport.requests.Request()

# Initialize the Firebase Admin app once at module scope (reused across warm
# invocations, mirroring the storage-client pattern). The project is pinned so
# token verification is bound to OUR Firebase tenant; logging it on init makes a
# project misconfiguration loud rather than a silent verify-against-wrong-tenant.
# Uses ADC on Cloud Run — no key file. Not yet wired into the handlers (U2).
firebase_admin.initialize_app(options={"projectId": PROJECT_ID})
logger.info("firebase_admin initialized for project %s", PROJECT_ID)


def _cors(response, status=200):
    """Attach CORS headers to every response."""
    if isinstance(response, tuple):
        body, code = response[0], response[1]
        return (body, code, CORS_HEADERS)
    return (response, status, CORS_HEADERS)


@functions_framework.http
def get_upload_urls(request):
    """Entry point: parse the request, route it via ``_dispatch``, and convert a
    GCS-level failure into a structured 503 instead of a bare 500.

    Every real-user action is token-gated (``verify_bearer``) and scoped to
    ``users/{uid}/…`` via the central ``resolve_prefix``. ``_dispatch`` enforces
    a strict action allow-list: anything not enumerated there is a 400, so a
    forgotten or removed legacy action (e.g. the retired ``get-index``) cannot
    survive as an unauthenticated reader. The storage guard below only ever
    answers 503 — it can turn a would-be crash into a denial, never into a
    served response, so the fail-closed posture is preserved.

    Actions (all Bearer-required):
    - (default) / "upload": POST {"recording": "...", "files": [...]}
    - "list":               POST {"action": "list"}
    - "sign-download":      POST {"action": "sign-download", "recording": "..."}
    """
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    data = request.get_json(silent=True)
    if not data:
        return _cors((jsonify({"error": "JSON body required"}), 400))

    action = data.get("action")

    try:
        return _dispatch(request, data, action)
    except google.api_core.exceptions.GoogleAPICallError as exc:
        # Every handler below reaches GCS, and a GCS-level failure (bucket
        # missing or SCREENCAP_BUCKET mis-set, IAM revoked, storage outage) is
        # NOT something the per-handler `except GoogleAuthError` catches — that
        # one covers signBlob credential refresh only. Uncaught, it escapes as a
        # bare 500 HTML traceback whose body names neither the action nor the
        # bucket, so the caller sees "something broke" and the operator has to
        # dig the stack trace out of Cloud Logging to find the bucket name.
        #
        # This is not hypothetical: a deploy that set SCREENCAP_BUCKET to a
        # bucket that did not exist took the public gallery AND share resolution
        # down for days behind exactly that opaque 500. GCS answers 403 (not
        # 404) for a nonexistent bucket so it cannot be told apart from a
        # revoked grant — hence one branch for both, with the bucket logged so
        # the misconfiguration is legible at a glance.
        logger.error(
            "GCS call failed (action=%r bucket=%r): %s", action, BUCKET, exc
        )
        return _cors((jsonify({"error": "Storage temporarily unavailable"}), 503))


def _dispatch(request, data, action):
    """Route one already-parsed request to its handler.

    Split out of ``get_upload_urls`` so the storage-failure guard there wraps
    EVERY handler uniformly — including any added later — rather than repeating
    a try/except at each GCS call site, where one omission silently restores the
    bare-500 behavior.
    """
    # Public demo actions — dispatched BEFORE any token-gated branch so a demo
    # request never touches verify_bearer-gated code. They hard-code the demo/
    # prefix via resolve_prefix(authenticated=False, ...) and honor no
    # client-supplied source/owner (defeats source=demo overloading).
    if action == "demo-list":
        return _handle_demo_list(data)
    if action == "demo-sign-download":
        return _handle_demo_sign_download(data)
    # Anonymous share resolution — dispatched BEFORE the token gate, like the
    # demo actions. It reads shares/ only (disjoint from users/ and demo/), never
    # touches verify_bearer-gated code, and signs only the artifacts named in the
    # stored record (never a path built from the raw token).
    if action == "resolve-share":
        return _handle_resolve_share(data)

    # Token-gated actions.
    if action in (None, "upload"):
        return _handle_upload(request, data)
    if action == "list":
        return _handle_list(request, data)
    if action == "sign-download":
        return _handle_sign_download(request, data)
    if action == "create-share":
        return _handle_create_share(request, data)
    if action == "revoke-share":
        return _handle_revoke_share(request, data)

    # Strict allow-list: unknown / removed actions are rejected, never silently
    # handled. ``get-index`` is gone — it read sessions/_index.json, a path the
    # migration retires; no v1 surface needs it.
    return _cors((jsonify({"error": f"Unknown action: {action!r}"}), 400))


def _authenticate(request) -> "tuple[str, None] | tuple[None, tuple]":
    """Verify the request's bearer token — the FIRST thing every gated handler does.

    Returns ``(uid, None)`` on success, or ``(None, response)`` where the
    response maps AuthInvalid -> 401 and AuthUnavailable -> 503. The annotated
    union makes the contract type-checkable: a handler that forgets
    ``if err: return err`` and passes ``uid=None`` into ``resolve_prefix`` is a
    type error. The 503 keeps a Firebase outage distinct from a hard deny so the
    client can fail closed. A CI contract test asserts no tokenless request ever
    reaches a GCS call, so this must run before any list/blob access.
    """
    try:
        return verify_bearer(request, PROJECT_ID), None
    except AuthUnavailable:
        return None, _cors(
            (jsonify({"error": "Auth verification temporarily unavailable"}), 503)
        )
    except AuthInvalid:
        return None, _cors((jsonify({"error": "Authentication required"}), 401))


def _paywall_enforced() -> bool:
    """Whether the signer denies uploads for accounts without an active sub.

    Read per-request from ``STRIPE_PAYWALL_ENFORCE`` (default off) so the
    entitlement-checking code can be deployed dark and the enforce flip is a
    config change, not a redeploy. Separate from the client-side
    ``SCREENCAP_STRIPE_PAYWALL`` flag so the two release tracks stay
    independent (billing plan KTD-6).
    """
    return os.environ.get("STRIPE_PAYWALL_ENFORCE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _authenticate_with_claims(request):
    """Verify the bearer token and ALSO return the decoded custom claims.

    Additive sibling of ``_authenticate`` used ONLY by the upload gate: returns
    ``(uid, claims, None)`` on success or ``(None, None, response)`` on failure,
    mapping AuthUnavailable -> 503 and AuthInvalid -> 401 identically.
    ``_handle_list`` / ``_handle_sign_download`` / demo paths keep the uid-only
    ``_authenticate`` (over ``verify_bearer``), so their contract and the
    tokenless-boundary contract test are untouched.
    """
    try:
        uid, claims = verify_bearer_full(request, PROJECT_ID)
    except AuthUnavailable:
        return None, None, _cors(
            (jsonify({"error": "Auth verification temporarily unavailable"}), 503)
        )
    except AuthInvalid:
        return None, None, _cors((jsonify({"error": "Authentication required"}), 401))
    return uid, claims, None


def _subscription_refusal(claims):
    """Return a 402 refusal if the paywall is enforced and the caller is not
    positively subscribed; ``None`` to allow.

    Fail-closed: signs only when ``subscribed`` reads exactly ``True``. A falsy,
    missing, or unreadable claim refuses and never falls through to signing
    (billing plan U2 / KTD-2). Downloads and list are never gated (R10).
    """
    if not _paywall_enforced():
        return None
    subscribed = claims.get("subscribed") if isinstance(claims, dict) else None
    if subscribed is True:
        return None
    return _cors(
        (jsonify({"error": "Subscription required", "code": "subscription_required"}), 402)
    )


def _collect_recordings(prefix, *, honor_unlisted, skip_invalid_names=False):
    """Tally per-recording file_count / total_size for blobs under an
    already-resolved prefix.

    Shared by the owner listing (``_handle_list``) and the demo gallery
    (``_handle_demo_list``). The demo/users NAMESPACE boundary lives in the
    ``resolve_prefix`` call at each call site; this helper only walks an
    already-validated prefix.

    ``honor_unlisted`` toggles a DELIBERATE divergence (plan: Key Technical
    Decisions). The owner's listing (``honor_unlisted=True``) hides a recording
    carrying a vestigial ``_unlisted`` marker; the demo gallery
    (``honor_unlisted=False``) is marker-BLIND so the superseded marker can never
    hide a curated recording. ``skip_invalid_names`` (demo only) drops a blob
    whose name would fail sign-download validation, so it is never
    listable-but-unplayable.
    """
    recordings = defaultdict(lambda: {"file_count": 0, "total_size": 0})
    plen = len(prefix)
    for blob in _storage_client.list_blobs(BUCKET, prefix=prefix, timeout=60):
        rel = blob.name[plen:]  # strip the known prefix -> {name}/{file...}
        parts = rel.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        rec_name = parts[0]
        if skip_invalid_names and not is_valid_name(rec_name):
            continue
        if parts[1] == "_unlisted":
            if honor_unlisted:
                recordings[rec_name]["unlisted"] = True
            continue
        recordings[rec_name]["file_count"] += 1
        recordings[rec_name]["total_size"] += blob.size or 0
    return [
        {"name": name, "file_count": info["file_count"], "total_size": info["total_size"]}
        for name, info in sorted(recordings.items())
        if not info.get("unlisted")
    ]


def _sign_recording(prefix, expiration):
    """List + sign GET URLs for every file under an already-resolved prefix.

    Returns the urls dict, or ``None`` if the prefix matched no blobs (the caller
    maps that to 404). The namespace boundary is enforced by ``resolve_prefix`` at
    the call site; this signs whatever the validated prefix matched, refreshing
    the signing credential ONCE before the per-blob signBlob loop. Propagates
    ``google.auth.exceptions.GoogleAuthError`` on a transient signing failure so
    callers can map it to 503.
    """
    blobs = list(_storage_client.list_blobs(BUCKET, prefix=prefix, timeout=60))
    if not blobs:
        return None
    _credentials.refresh(_auth_request)
    urls = {}
    for blob in blobs:
        rel_name = blob.name[len(prefix):]
        if not rel_name:
            continue
        urls[rel_name] = blob.generate_signed_url(
            version="v4",
            expiration=expiration,
            method="GET",
            service_account_email=_credentials.service_account_email,
            access_token=_credentials.token,
        )
    return urls


def _handle_demo_list(data):
    """List the public demo gallery — unauthenticated, demo/ only.

    A physically separate code path from the token-gated handlers; the demo/users
    boundary is the ``resolve_prefix`` call below. Marker-BLIND
    (``honor_unlisted=False``) and name-guarded (``skip_invalid_names=True``). See
    ``_handle_list`` for the owner-side counterpart, which DOES honor ``_unlisted``
    — the divergence is deliberate (plan Key Technical Decisions).
    """
    prefix = resolve_prefix(authenticated=False, uid=None, source=None, name=None)  # "demo/"
    result = _collect_recordings(prefix, honor_unlisted=False, skip_invalid_names=True)
    return _cors(jsonify({"recordings": result}))


def _handle_demo_sign_download(data):
    """Signed GET URLs for a public demo recording — unauthenticated, demo/ only."""
    recording = data.get("recording")
    if not recording:
        return _cors((jsonify({"error": "'recording' field required"}), 400))

    try:
        # Hard-coded demo/ namespace; resolve_prefix raises on any climbing name.
        prefix = resolve_prefix(authenticated=False, uid=None, source=None, name=recording)
    except PrefixResolutionError:
        return _cors((jsonify({"error": "Invalid recording name"}), 400))

    try:
        urls = _sign_recording(prefix, timedelta(hours=DEMO_DOWNLOAD_EXPIRY_HOURS))
    except google.auth.exceptions.GoogleAuthError as exc:
        logger.warning("signing credential refresh/sign failed: %s", exc)
        return _cors((jsonify({"error": "Signing temporarily unavailable"}), 503))
    if urls is None:
        return _cors((jsonify({"error": f"Recording not found: {recording}"}), 404))
    return _cors(jsonify({"urls": urls, "gcs_prefix": f"gs://{BUCKET}/{prefix}"}))


def _handle_list(request, data):
    """List the caller's own recordings (or sessions), scoped to their namespace.

    Owner-side counterpart to ``_handle_demo_list``: this DOES honor the vestigial
    ``_unlisted`` marker (hides the recording), whereas the demo gallery is
    marker-blind. The divergence is deliberate (plan Key Technical Decisions).
    """
    uid, err = _authenticate(request)
    if err:
        return err

    data = data or {}
    source = data.get("source", "recordings")
    try:
        prefix = resolve_prefix(authenticated=True, uid=uid, source=source, name=None)
    except PrefixResolutionError:
        return _cors((jsonify({"error": "Invalid source"}), 400))

    result = _collect_recordings(prefix, honor_unlisted=True)
    return _cors(jsonify({"recordings": result}))


def _handle_sign_download(request, data):
    """Generate signed GET URLs for one of the caller's own recordings."""
    uid, err = _authenticate(request)
    if err:
        return err

    recording = data.get("recording")
    if not recording:
        return _cors((jsonify({"error": "'recording' field required"}), 400))

    source = data.get("source", "recordings")
    try:
        prefix = resolve_prefix(authenticated=True, uid=uid, source=source, name=recording)
    except PrefixResolutionError:
        return _cors((jsonify({"error": "Invalid request parameters"}), 400))

    try:
        urls = _sign_recording(prefix, timedelta(minutes=USER_DOWNLOAD_EXPIRY_MINUTES))
    except google.auth.exceptions.GoogleAuthError as exc:
        logger.warning("signing credential refresh/sign failed: %s", exc)
        return _cors((jsonify({"error": "Signing temporarily unavailable"}), 503))
    if urls is None:
        # Another user's name does not exist under THIS caller's prefix -> 404.
        # AE2: a cross-user download is indistinguishable from "not found", and
        # no URL is ever signed for it.
        return _cors((jsonify({"error": f"Recording not found: {recording}"}), 404))
    return _cors(jsonify({"urls": urls, "gcs_prefix": f"gs://{BUCKET}/{prefix}"}))


def _handle_upload(request, data):
    """Generate signed PUT URLs under the caller's own namespace."""
    uid, claims, err = _authenticate_with_claims(request)
    if err:
        return err

    # Entitlement gate (upload-only). Fail-closed when enforced; a no-op when
    # STRIPE_PAYWALL_ENFORCE is off. Runs before any signing work is done.
    refusal = _subscription_refusal(claims)
    if refusal:
        return refusal

    recording = data.get("recording")
    files = data.get("files")
    if not recording or not files:
        return _cors((jsonify({"error": "'recording' and 'files' fields required"}), 400))

    if len(files) > MAX_FILES:
        return _cors((jsonify({"error": f"Too many files (max {MAX_FILES})"}), 400))

    source = data.get("source", "recordings")
    try:
        prefix = resolve_prefix(authenticated=True, uid=uid, source=source, name=recording)
    except PrefixResolutionError:
        return _cors((jsonify({"error": "Invalid request parameters"}), 400))

    # First pass: validate names and find which files actually need a signed PUT
    # (an object that already exists is skipped). The signing credential is then
    # refreshed ONCE before signing — not once per file as before — so a
    # MAX_FILES upload no longer does up to 500 token refreshes.
    urls = {}
    to_sign = []
    for f in files:
        name = f.get("name")
        if not name:
            continue
        if not _FILENAME_RE.match(name) or ".." in name:
            continue
        blob = _bucket.blob(f"{prefix}{name}")
        if blob.exists():
            urls[name] = None
        else:
            to_sign.append((name, blob, f.get("content_type", "application/octet-stream")))

    if to_sign:
        try:
            _credentials.refresh(_auth_request)
            for name, blob, content_type in to_sign:
                urls[name] = blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(minutes=UPLOAD_EXPIRY_MINUTES),
                    method="PUT",
                    content_type=content_type,
                    service_account_email=_credentials.service_account_email,
                    access_token=_credentials.token,
                )
        except google.auth.exceptions.GoogleAuthError as exc:
            # A transient signing failure -> 503 with a JSON body, not a bare 500
            # with a partial URL batch, so the client can tell it apart and retry.
            logger.warning("signing credential refresh/sign failed: %s", exc)
            return _cors((jsonify({"error": "Signing temporarily unavailable"}), 503))

    return _cors(jsonify({"urls": urls, "gcs_prefix": f"gs://{BUCKET}/{prefix}"}))


# ---------------------------------------------------------------------------
# Share-by-link (SCR-229) — a per-recording, token-addressable share namespace
# disjoint from users/ and demo/. Only the artifacts named in the stored record
# are ever signed, and the anonymous resolver reads shares/ only.
# ---------------------------------------------------------------------------


def _handle_create_share(request, data):
    """Mint a share: write the record + return signed PUT URLs (Bearer required).

    The token is server-generated (unguessable) and bound to the caller's uid in
    the record so ``revoke-share`` can enforce owner-only revocation. The
    per-share decryption key never reaches here — it stays in the URL fragment,
    minted client-side.
    """
    uid, err = _authenticate(request)
    if err:
        return err

    files = data.get("files")
    if not files or not isinstance(files, list):
        return _cors((jsonify({"error": "'files' field required"}), 400))
    if len(files) > MAX_FILES:
        return _cors((jsonify({"error": f"Too many files (max {MAX_FILES})"}), 400))
    for name in files:
        if not shares.is_valid_artifact(name):
            return _cors((jsonify({"error": f"Invalid artifact name: {name!r}"}), 400))

    days = data.get("expires_days")
    try:
        days = int(days) if days is not None else shares.DEFAULT_EXPIRY_DAYS
    except (TypeError, ValueError):
        return _cors((jsonify({"error": "Invalid expires_days"}), 400))
    if not 1 <= days <= 365:
        return _cors((jsonify({"error": "expires_days out of range (1-365)"}), 400))

    expires_at = shares.expires_at_iso(datetime.now(timezone.utc), days)
    token = shares.new_token()
    record = shares.build_record(uid, list(files), expires_at, view_only=True)

    try:
        _bucket.blob(shares.record_key(token)).upload_from_string(
            shares.dumps(record), content_type="application/json"
        )
        _credentials.refresh(_auth_request)
        urls = {}
        for name in files:
            blob = _bucket.blob(shares.artifact_key(token, name))
            urls[name] = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=UPLOAD_EXPIRY_MINUTES),
                method="PUT",
                content_type="application/octet-stream",
                service_account_email=_credentials.service_account_email,
                access_token=_credentials.token,
            )
    except google.auth.exceptions.GoogleAuthError as exc:
        logger.warning("share create signing failed: %s", exc)
        return _cors((jsonify({"error": "Signing temporarily unavailable"}), 503))

    return _cors(
        jsonify(
            {"token": token, "urls": urls, "expires_at": expires_at, "view_only": True}
        )
    )


def _handle_resolve_share(data):
    """Resolve a share token to signed GET URLs — UNAUTHENTICATED, shares/ only.

    The token is an opaque lookup key; this signs only the artifacts named in the
    stored record, never a path built from the raw token. A revoked or expired
    share returns 410 so the viewer can show the denied state; an unknown token
    is 404.
    """
    token = data.get("token")
    if not shares.is_valid_token(token):
        return _cors((jsonify({"error": "Invalid share token"}), 400))

    record_blob = _bucket.blob(shares.record_key(token))
    if not record_blob.exists():
        return _cors((jsonify({"error": "Share not found"}), 404))
    try:
        record = shares.loads(record_blob.download_as_text())
    except shares.ShareError:
        return _cors((jsonify({"error": "Share not found"}), 404))

    if shares.is_revoked(record) or shares.is_expired(record, datetime.now(timezone.utc)):
        return _cors(
            (jsonify({"error": "Share no longer available", "code": "share_gone"}), 410)
        )

    try:
        _credentials.refresh(_auth_request)
        urls = {}
        for name in record.get("artifacts", []):
            if not shares.is_valid_artifact(name):
                continue
            blob = _bucket.blob(shares.artifact_key(token, name))
            urls[name] = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=SHARE_DOWNLOAD_EXPIRY_HOURS),
                method="GET",
                service_account_email=_credentials.service_account_email,
                access_token=_credentials.token,
            )
    except google.auth.exceptions.GoogleAuthError as exc:
        logger.warning("share resolve signing failed: %s", exc)
        return _cors((jsonify({"error": "Signing temporarily unavailable"}), 503))

    return _cors(
        jsonify(
            {
                "urls": urls,
                "view_only": bool(record.get("view_only", True)),
                "expires_at": record.get("expires_at"),
            }
        )
    )


def _handle_revoke_share(request, data):
    """Revoke a share (Bearer required, owner-only)."""
    uid, err = _authenticate(request)
    if err:
        return err

    token = data.get("token")
    if not shares.is_valid_token(token):
        return _cors((jsonify({"error": "Invalid share token"}), 400))

    record_blob = _bucket.blob(shares.record_key(token))
    if not record_blob.exists():
        return _cors((jsonify({"error": "Share not found"}), 404))
    try:
        record = shares.loads(record_blob.download_as_text())
    except shares.ShareError:
        return _cors((jsonify({"error": "Share not found"}), 404))

    if not shares.is_owner(record, uid):
        # A non-owner cannot tell "not yours" from "does not exist".
        return _cors((jsonify({"error": "Share not found"}), 404))

    record["revoked"] = True
    _bucket.blob(shares.record_key(token)).upload_from_string(
        shares.dumps(record), content_type="application/json"
    )
    return _cors(jsonify({"revoked": True, "token": token}))
