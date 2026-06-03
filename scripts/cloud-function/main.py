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

    # Deploy (set SCREENCAP_BUCKET to the staging bucket pre-cutover; the prod
    # default is the unset 'screencap-recordings'):
    gcloud functions deploy get-upload-urls \
        --project proteus-photos --gen2 \
        --runtime python312 \
        --trigger-http \
        --allow-unauthenticated \
        --region southamerica-east1 \
        --source scripts/cloud-function/ \
        --entry-point get_upload_urls \
        --service-account "$SIGNER" \
        --set-env-vars SCREENCAP_BUCKET=screencap-recordings-staging
    # The dev function (get-upload-urls-dev) is identical with
    # SCREENCAP_BUCKET=screencap-recordings-dev-staging.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta

import firebase_admin
import functions_framework
import google.auth
import google.auth.transport.requests
from auth import AuthInvalid, AuthUnavailable, verify_bearer
from flask import jsonify
from google.cloud import storage
from paths import PrefixResolutionError, resolve_prefix

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

# Firebase project the signing function trusts. PINNED explicitly so the
# function can never verify-but-misattribute a token from a foreign Firebase
# project: verify_bearer (auth.py) re-asserts the decoded token's aud/iss
# against this value, over and above the SDK's own check.
PROJECT_ID = (
    os.environ.get("SCREENCAP_PROJECT_ID")
    or os.environ.get("GOOGLE_CLOUD_PROJECT")
    or "proteus-photos"
)

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

    # Strict allow-list: unknown / removed actions are rejected, never silently
    # handled. ``get-index`` is gone — it read sessions/_index.json, a path the
    # migration retires; no v1 surface needs it.
    return _cors((jsonify({"error": f"Unknown action: {action!r}"}), 400))


def _authenticate(request):
    """Verify the request's bearer token — the FIRST thing every gated handler does.

    Returns ``(uid, None)`` on success, or ``(None, response)`` where the
    response maps AuthInvalid -> 401 and AuthUnavailable -> 503. The 503 keeps a
    Firebase outage distinct from a hard deny so the client can fail closed. A CI
    contract test asserts no tokenless request ever reaches a GCS call, so this
    must run before any list/blob access.
    """
    try:
        return verify_bearer(request, PROJECT_ID), None
    except AuthUnavailable:
        return None, _cors(
            (jsonify({"error": "Auth verification temporarily unavailable"}), 503)
        )
    except AuthInvalid:
        return None, _cors((jsonify({"error": "Authentication required"}), 401))


def _handle_demo_list(data):
    """List the public demo gallery — unauthenticated, demo/ only.

    A physically separate code path from the token-gated handlers (a bug in one
    cannot cross into the other) and reachable without a token. Marker-BLIND:
    a migrated recording may carry a vestigial _unlisted blob, but the marker is
    superseded by namespace isolation and must NOT hide a curated demo recording.
    """
    from collections import defaultdict

    prefix = resolve_prefix(authenticated=False, uid=None, source=None, name=None)  # "demo/"
    recordings = defaultdict(lambda: {"file_count": 0, "total_size": 0})

    plen = len(prefix)
    blobs = _storage_client.list_blobs(BUCKET, prefix=prefix, timeout=60)
    for blob in blobs:
        # blob.name = "demo/{name}/{filename...}"
        rel = blob.name[plen:]
        parts = rel.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        rec_name = parts[0]
        if parts[1] == "_unlisted":
            # Skip the marker file itself, but do NOT hide the recording.
            continue
        recordings[rec_name]["file_count"] += 1
        recordings[rec_name]["total_size"] += blob.size or 0

    result = [
        {"name": name, "file_count": info["file_count"], "total_size": info["total_size"]}
        for name, info in sorted(recordings.items())
    ]
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

    blobs = list(_storage_client.list_blobs(BUCKET, prefix=prefix, timeout=60))
    if not blobs:
        return _cors((jsonify({"error": f"Recording not found: {recording}"}), 404))

    _credentials.refresh(_auth_request)

    urls = {}
    for blob in blobs:
        rel_name = blob.name[len(prefix):]
        if not rel_name:
            continue
        urls[rel_name] = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=DEMO_DOWNLOAD_EXPIRY_HOURS),
            method="GET",
            service_account_email=_credentials.service_account_email,
            access_token=_credentials.token,
        )

    return _cors(jsonify({"urls": urls, "gcs_prefix": f"gs://{BUCKET}/{prefix}"}))


def _handle_list(request, data):
    """List the caller's own recordings (or sessions), scoped to their namespace."""
    from collections import defaultdict

    uid, err = _authenticate(request)
    if err:
        return err

    data = data or {}
    source = data.get("source", "recordings")
    try:
        prefix = resolve_prefix(authenticated=True, uid=uid, source=source, name=None)
    except PrefixResolutionError:
        return _cors((jsonify({"error": "Invalid source"}), 400))

    recordings = defaultdict(lambda: {"file_count": 0, "total_size": 0})

    plen = len(prefix)
    blobs = _storage_client.list_blobs(BUCKET, prefix=prefix, timeout=60)
    for blob in blobs:
        # blob.name = "users/{uid}/{source}/{name}/{filename...}". Strip the
        # already-known prefix so the FIRST remaining segment is the recording
        # name (not the uid) — the re-based parsing the global layout didn't need.
        rel = blob.name[plen:]
        parts = rel.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        rec_name = parts[0]
        # _unlisted is vestigial (superseded by namespace isolation) but still
        # honored here for the owner's own listing.
        if parts[1] == "_unlisted":
            recordings[rec_name]["unlisted"] = True
            continue
        recordings[rec_name]["file_count"] += 1
        recordings[rec_name]["total_size"] += blob.size or 0

    result = [
        {"name": name, "file_count": info["file_count"], "total_size": info["total_size"]}
        for name, info in sorted(recordings.items())
        if not info.get("unlisted")
    ]
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

    blobs = list(_storage_client.list_blobs(BUCKET, prefix=prefix, timeout=60))
    if not blobs:
        # Another user's name does not exist under THIS caller's prefix -> 404.
        # AE2: a cross-user download is indistinguishable from "not found", and
        # no URL is ever signed for it.
        return _cors((jsonify({"error": f"Recording not found: {recording}"}), 404))

    _credentials.refresh(_auth_request)

    urls = {}
    for blob in blobs:
        rel_name = blob.name[len(prefix):]
        if not rel_name:
            continue
        urls[rel_name] = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=USER_DOWNLOAD_EXPIRY_MINUTES),
            method="GET",
            service_account_email=_credentials.service_account_email,
            access_token=_credentials.token,
        )

    return _cors(jsonify({"urls": urls, "gcs_prefix": f"gs://{BUCKET}/{prefix}"}))


def _handle_upload(request, data):
    """Generate signed PUT URLs under the caller's own namespace."""
    uid, err = _authenticate(request)
    if err:
        return err

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

    urls = {}
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
            _credentials.refresh(_auth_request)
            urls[name] = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=UPLOAD_EXPIRY_MINUTES),
                method="PUT",
                content_type=f.get("content_type", "application/octet-stream"),
                service_account_email=_credentials.service_account_email,
                access_token=_credentials.token,
            )

    return _cors(jsonify({"urls": urls, "gcs_prefix": f"gs://{BUCKET}/{prefix}"}))
