"""Cloud Function: signed GCS URLs for screencap recording upload/download.

Supports three actions (dispatched via ``action`` field in JSON body):
- (default/no action) — generate signed upload URLs
- ``list`` — list available recordings in the bucket
- ``sign-download`` — generate signed download URLs for a recording

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
from flask import jsonify
from google.cloud import storage

logger = logging.getLogger(__name__)

BUCKET = os.environ.get("SCREENCAP_BUCKET", "screencap-recordings")
UPLOAD_EXPIRY_MINUTES = 15
DOWNLOAD_EXPIRY_HOURS = 4
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

# Only allow safe characters in recording and file names.
# Slashes allowed in file names (for subdirectories like screenshots/0.png).
_RECORDING_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$")
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
    """Dispatch to upload, list, or sign-download handlers.

    Actions:
    - (default) upload: POST {"recording": "...", "files": [...]}
    - list:             POST {"action": "list"}
    - sign-download:    POST {"action": "sign-download", "recording": "..."}
    """
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    data = request.get_json(silent=True)
    if not data:
        return _cors((jsonify({"error": "JSON body required"}), 400))

    action = data.get("action")

    if action == "get-index":
        return _handle_get_index(data)
    if action == "list":
        return _handle_list(data)
    if action == "sign-download":
        return _handle_sign_download(data)
    return _handle_upload(data)


def _handle_get_index(data=None):
    """Return the cross-recording session index."""
    import json
    blob = _bucket.blob("sessions/_index.json")
    try:
        raw = blob.download_as_bytes()
        index = json.loads(raw)
        return _cors(jsonify(index))
    except Exception:
        return _cors(jsonify({"version": 1, "recordings": {}, "total_recordings": 0}))


def _handle_list(data=None):
    """List available recordings (or sessions) in the bucket."""
    from collections import defaultdict

    data = data or {}
    source = data.get("source", "recordings")
    if source not in ("recordings", "sessions"):
        return _cors((jsonify({"error": "Invalid source"}), 400))

    recordings = defaultdict(lambda: {"file_count": 0, "total_size": 0})

    blobs = _storage_client.list_blobs(BUCKET, prefix=f"{source}/", timeout=60)
    for blob in blobs:
        # blob.name = "{source}/{name}/{filename...}"
        parts = blob.name.split("/", 2)
        if len(parts) < 3 or not parts[2]:
            continue
        rec_name = parts[1]
        if parts[2] == "_unlisted":
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


def _handle_sign_download(data):
    """Generate signed GET URLs for all files in a recording (or session)."""
    recording = data.get("recording")
    if not recording:
        return _cors((jsonify({"error": "'recording' field required"}), 400))

    if not _RECORDING_RE.match(recording):
        return _cors((jsonify({"error": "Invalid recording name"}), 400))

    source = data.get("source", "recordings")
    if source not in ("recordings", "sessions"):
        return _cors((jsonify({"error": "Invalid source"}), 400))

    prefix = f"{source}/{recording}/"
    blobs = list(_storage_client.list_blobs(BUCKET, prefix=prefix, timeout=60))

    if not blobs:
        return _cors((jsonify({"error": f"Recording not found: {recording}"}), 404))

    _credentials.refresh(_auth_request)

    urls = {}
    for blob in blobs:
        # Strip the prefix to get the relative filename
        rel_name = blob.name[len(prefix):]
        if not rel_name:
            continue
        urls[rel_name] = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=DOWNLOAD_EXPIRY_HOURS),
            method="GET",
            service_account_email=_credentials.service_account_email,
            access_token=_credentials.token,
        )

    return _cors(jsonify({
        "urls": urls,
        "gcs_prefix": f"gs://{BUCKET}/{prefix}",
    }))


def _handle_upload(data):
    """Generate signed upload URLs for recording files (original behavior)."""
    recording = data.get("recording")
    files = data.get("files")
    if not recording or not files:
        return _cors((jsonify({"error": "'recording' and 'files' fields required"}), 400))

    if not _RECORDING_RE.match(recording):
        return _cors((jsonify({"error": "Invalid recording name"}), 400))

    if len(files) > MAX_FILES:
        return _cors((jsonify({"error": f"Too many files (max {MAX_FILES})"}), 400))

    urls = {}
    for f in files:
        name = f.get("name")
        if not name:
            continue
        if not _FILENAME_RE.match(name) or ".." in name:
            continue
        blob = _bucket.blob(f"recordings/{recording}/{name}")
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

    return _cors(jsonify({
        "urls": urls,
        "gcs_prefix": f"gs://{BUCKET}/recordings/{recording}/",
    }))
