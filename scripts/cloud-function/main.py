"""Cloud Function: generate signed GCS upload URLs for screencap recordings.

Deploy:
    # 1. Grant signing permission to the Cloud Function's service account:
    gcloud functions describe get-upload-urls --region southamerica-east1 \
        --format='value(serviceConfig.serviceAccountEmail)'
    # Then grant it signBlob:
    gcloud projects add-iam-policy-binding PROJECT_ID \
        --member='serviceAccount:SA_EMAIL' \
        --role='roles/iam.serviceAccountTokenCreator'

    # 2. Deploy:
    gcloud functions deploy get-upload-urls \
        --runtime python312 \
        --trigger-http \
        --allow-unauthenticated \
        --region southamerica-east1 \
        --source scripts/cloud-function/ \
        --entry-point get_upload_urls
"""

from __future__ import annotations

import re
from datetime import timedelta

import functions_framework
import google.auth
import google.auth.transport.requests
from flask import jsonify, request
from google.cloud import storage

BUCKET = "screencap-recordings"
EXPIRY_MINUTES = 15
MAX_FILES = 500

# Only allow safe characters in recording and file names.
# Slashes allowed in file names (for subdirectories like screenshots/0.png).
_RECORDING_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$")
_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,511}$")

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


def _cors(response, status=200):
    """Attach CORS headers to every response."""
    if isinstance(response, tuple):
        body, code = response[0], response[1]
        return (body, code, CORS_HEADERS)
    return (response, status, CORS_HEADERS)


@functions_framework.http
def get_upload_urls(request):
    """Generate signed upload URLs for recording files.

    Request:
        POST {"recording": "my-rec",
              "files": [{"name": "video.mp4", "content_type": "video/mp4"}, ...]}

    Response:
        {"urls": {"video.mp4": "https://..." or null}, "gcs_prefix": "gs://..."}

    Files that already exist in the bucket get ``null`` (skip).
    """
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    data = request.get_json(silent=True)
    if not data:
        return _cors((jsonify({"error": "JSON body required"}), 400))

    recording = data.get("recording")
    files = data.get("files")
    if not recording or not files:
        return _cors((jsonify({"error": "'recording' and 'files' fields required"}), 400))

    # Validate recording name
    if not _RECORDING_RE.match(recording):
        return _cors((jsonify({"error": "Invalid recording name"}), 400))

    if len(files) > MAX_FILES:
        return _cors((jsonify({"error": f"Too many files (max {MAX_FILES})"}), 400))

    urls = {}
    for f in files:
        name = f.get("name")
        if not name:
            continue
        # Validate filename (no path traversal)
        if not _FILENAME_RE.match(name) or ".." in name:
            continue
        blob = _bucket.blob(f"recordings/{recording}/{name}")
        if blob.exists():
            urls[name] = None
        else:
            # Refresh credentials to get a valid access token for IAM signing
            _credentials.refresh(_auth_request)
            urls[name] = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=EXPIRY_MINUTES),
                method="PUT",
                content_type=f.get("content_type", "application/octet-stream"),
                service_account_email=_credentials.service_account_email,
                access_token=_credentials.token,
            )

    return _cors(jsonify({
        "urls": urls,
        "gcs_prefix": f"gs://{BUCKET}/recordings/{recording}/",
    }))
