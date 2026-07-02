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
- (default/no action) / ``upload`` — signed PUT URLs under the caller's namespace,
  gated on an active cloud entitlement (U2)
- ``list`` — list the caller's own recordings
- ``sign-download`` — signed GET URLs for one of the caller's own recordings
- ``grant-founding`` — grant the caller the founding cloud entitlement (U1;
  self-service in v1, must move behind the payment webhook when billing lands)

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

    # Deploy (set SCREENCAP_BUCKET to the staging bucket pre-cutover; the prod
    # default is the unset 'screencap-recordings'). ALWAYS set
    # SCREENCAP_PROJECT_ID explicitly — it is the only thing that pins the
    # trusted Firebase project; there is NO ambient fallback (see
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
from datetime import timedelta

import firebase_admin
import functions_framework
import google.auth
import google.auth.exceptions
import google.auth.transport.requests
from auth import AuthInvalid, AuthUnavailable, verify_bearer
from entitlements import EntitlementUnavailable, grant_founding, read_entitlement
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
    """Dispatch to the upload / list / sign-download handlers.

    Every real-user action is token-gated (``verify_bearer``) and scoped to
    ``users/{uid}/…`` via the central ``resolve_prefix``. The dispatcher enforces
    a strict action allow-list: anything not enumerated here is a 400, so a
    forgotten or removed legacy action (e.g. the retired ``get-index``) cannot
    survive as an unauthenticated reader.

    Actions (all Bearer-required):
    - (default) / "upload": POST {"recording": "...", "files": [...]}
    - "list":               POST {"action": "list"}
    - "sign-download":      POST {"action": "sign-download", "recording": "..."}
    - "grant-founding":     POST {"action": "grant-founding"}
    """
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    data = request.get_json(silent=True)
    if not data:
        return _cors((jsonify({"error": "JSON body required"}), 400))

    action = data.get("action")

    # Public demo actions — dispatched BEFORE any token-gated branch so a demo
    # request never touches verify_bearer-gated code. They hard-code the demo/
    # prefix via resolve_prefix(authenticated=False, ...) and honor no
    # client-supplied source/owner (defeats source=demo overloading).
    if action == "demo-list":
        return _handle_demo_list(data)
    if action == "demo-sign-download":
        return _handle_demo_sign_download(data)

    # Token-gated, users/{uid}/-scoped actions.
    if action in (None, "upload"):
        return _handle_upload(request, data)
    if action == "list":
        return _handle_list(request, data)
    if action == "sign-download":
        return _handle_sign_download(request, data)
    if action == "grant-founding":
        return _handle_grant_founding(request, data)

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


def _handle_grant_founding(request, data):
    """Grant the founding plan to the authenticated caller (U1).

    Token-gated like every users/-scoped action: ``_authenticate`` runs FIRST so
    no entitlement is ever written without a verified uid. v1 founding is free
    and self-authorized; when billing activates this grant MUST move behind the
    payment-processor webhook / server-side eligibility check and must not remain
    client-callable (plan Definition of Done). Idempotent — a repeat grant re-sets
    the same claim. A transient Firebase failure is a 503 (retryable), never a
    partial success.
    """
    uid, err = _authenticate(request)
    if err:
        return err
    try:
        entitlement = grant_founding(uid)
    except EntitlementUnavailable as exc:
        logger.warning("founding grant failed: %s", exc)
        return _cors((jsonify({"error": "Entitlement grant temporarily unavailable"}), 503))
    return _cors(jsonify({"entitlement": entitlement}))


def _handle_upload(request, data):
    """Generate signed PUT URLs under the caller's own namespace.

    The authoritative entitlement gate (U2) lives here: after the uid is
    resolved, the caller's plan is read LIVE (``read_entitlement`` ->
    ``get_user``) — never off the already-verified token, whose claims are
    discarded — and a non-entitled caller is rejected with 403 (a hard, permanent
    block) BEFORE any URL is signed. A lookup failure is 503 (transient) so the
    client can tell a permanent block from something retryable and fail closed.
    This is the trust boundary; the daemon-side pre-check (U4) is only a friendly
    fast-fail and is never the gate.
    """
    uid, err = _authenticate(request)
    if err:
        return err

    try:
        entitlement = read_entitlement(uid)
    except EntitlementUnavailable as exc:
        logger.warning("entitlement lookup failed: %s", exc)
        return _cors(
            (jsonify({"error": "Entitlement verification temporarily unavailable"}), 503)
        )
    if not entitlement["active"]:
        # Signed in but no active plan: fail-closed, no URLs signed. 403 (not 401)
        # so the client distinguishes "not entitled" from "not authenticated".
        return _cors(
            (jsonify({"error": "Cloud upload requires an active plan", "plan": entitlement["plan"]}), 403)
        )

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
