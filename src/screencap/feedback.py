"""Client for the in-app feedback relay (feat/in-app-feedback-form, U2).

The macOS app never makes cloud HTTPS calls itself (the standing invariant —
all cloud traffic goes through this bundled CLI). ``screencap feedback send``
reads a JSON payload on stdin, carries it to the ``submit-feedback`` relay
(``scripts/cloud-function/feedback.py``), and prints a JSON envelope on stdout.

The command ALWAYS exits 0 for expected failures, emitting
``{"ok": false, "error_kind": ..., "retryable": ...}`` — Swift maps each kind to
distinct copy (KTD-7). Attachment bytes go straight to Linear's signed URL
(never through the relay), after the returned ``uploadUrl`` host is checked
against the same allowlist the relay uses.

Privacy (R10): this module reads ONLY the exact attachment paths the caller
supplies (via ``_read_file``), never the recordings tree or ``recording.db``.
"""

from __future__ import annotations

import os

import requests

# --------------------------------------------------------------------------
# Mirrored constants (KTD-5) — kept EQUAL to scripts/cloud-function/feedback.py.
# The cross-runtime equality test (tests/test_feedback.py) pins these.
# --------------------------------------------------------------------------
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
MAX_ATTACHMENTS = 5

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/heic",
        "video/mp4",
        "video/quicktime",
    }
)

LINEAR_UPLOAD_HOSTS = frozenset({"uploads.linear.app"})

# U0 finding (SCR-281): the real workspace signs PUT URLs in GCS path style —
# https://storage.googleapis.com/uploads.linear.app/<path> — so the upload
# target is allowed EITHER on the Linear host or on GCS pinned to Linear's
# bucket path. assetUrls stay uploads.linear.app-only (LINEAR_UPLOAD_HOSTS).
LINEAR_UPLOAD_GCS_HOST = "storage.googleapis.com"
LINEAR_UPLOAD_GCS_PREFIX = "/uploads.linear.app/"

# gen2 function deployed by name in the billing/signing project (mirrors the
# cloudfunctions.net convention upload.py uses). Overridable for tests / staging.
DEFAULT_FEEDBACK_URL = (
    "https://southamerica-east1-proteus-photos.cloudfunctions.net/submit-feedback"
)

_HTTP_TIMEOUT = (10, 300)  # connect/read, mirrors upload.py


def _relay_url() -> str:
    return os.environ.get("SCREENCAP_FEEDBACK_URL", DEFAULT_FEEDBACK_URL)


def _read_file(path: str) -> bytes:
    """Read one caller-supplied attachment. The ONLY filesystem seam (R10)."""
    with open(path, "rb") as fh:
        return fh.read()


def _envelope(ok: bool, **fields) -> dict:
    out = {"ok": ok}
    out.update(fields)
    return out


def _fail(kind: str, message: str, *, retryable: bool) -> dict:
    return _envelope(False, error_kind=kind, message=message, retryable=retryable)


# Which relay error kinds the user can usefully retry. ``expired`` is retryable
# (KTD-3/KTD-7): a claim that lapses mid-upload just means "re-prepare and try
# again" — a slow-uplink honest path, not a permanent rejection.
_RETRYABLE_KINDS = {"network", "server", "expired"}


def _upload_target_allowed(url: str) -> bool:
    """True iff ``url`` may receive attachment bytes: https on a Linear upload
    host, or GCS path-style pinned to Linear's bucket (either way, bytes can
    only land in Linear-controlled storage)."""
    from urllib.parse import urlparse

    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.hostname in LINEAR_UPLOAD_HOSTS:
        return True
    if parts.hostname != LINEAR_UPLOAD_GCS_HOST:
        return False
    # Reject dot-segments before the prefix check: requests/urllib3 collapse
    # "/../" per RFC 3986 BEFORE sending, so a path that passes a raw prefix
    # check (".../uploads.linear.app/../other-bucket/x") is rewritten to a
    # DIFFERENT bucket by the time bytes go out. Linear's real signed URLs are
    # all UUID path segments with no dot-segments, so this only rejects forged
    # targets.
    if any(seg in ("..", ".") for seg in parts.path.split("/")):
        return False
    return parts.path.startswith(LINEAR_UPLOAD_GCS_PREFIX)


def _validate_attachments(attachments: list[dict]) -> list[dict] | dict:
    """Client-side cap/type check (mirrors the relay). Returns a fail envelope
    on rejection so the user learns at selection time, never at submit."""
    if len(attachments) > MAX_ATTACHMENTS:
        return _fail("invalid", f"at most {MAX_ATTACHMENTS} attachments", retryable=False)
    total = 0
    resolved = []
    for att in attachments:
        path = att.get("path")
        content_type = att.get("content_type")
        if content_type not in ALLOWED_CONTENT_TYPES:
            return _fail("invalid", "unsupported attachment type", retryable=False)
        try:
            size = os.path.getsize(path)
        except OSError:
            return _fail("invalid", "attachment not found", retryable=False)
        if size > MAX_FILE_BYTES:
            return _fail("too_large", "attachment over the size limit", retryable=False)
        total += size
        if total > MAX_TOTAL_BYTES:
            return _fail("too_large", "attachments over the total size limit", retryable=False)
        resolved.append({"path": path, "content_type": content_type, "size": size})
    return resolved


def _relay_post(action: str, payload: dict) -> dict:
    """POST to the relay; normalize transport failures to a ``network`` envelope.

    A relay ``{ok: false, error_kind}`` body is returned as-is (with retryable
    filled from the kind) so the taxonomy flows through unchanged.
    """
    body = dict(payload)
    body["action"] = action
    try:
        resp = requests.post(
            _relay_url(),
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
    except (requests.ConnectionError, requests.Timeout):
        return _fail("network", "you appear to be offline", retryable=True)
    except requests.RequestException:
        return _fail("network", "could not reach the feedback service", retryable=True)
    try:
        data = resp.json()
    except ValueError:
        return _fail("server", "unexpected response", retryable=True)
    if not isinstance(data, dict):
        return _fail("server", "unexpected response", retryable=True)
    if data.get("ok"):
        return data
    kind = data.get("error_kind", "server")
    return _fail(kind, data.get("message", "request failed"), retryable=kind in _RETRYABLE_KINDS)


def _put_file(upload: dict, path: str) -> dict | None:
    """PUT one file's bytes to Linear's signed URL. Returns a fail envelope or
    None on success. The upload host is validated before any bytes are sent."""
    upload_url = upload.get("uploadUrl", "")
    if not _upload_target_allowed(upload_url):
        return _fail("server", "unexpected upload target", retryable=True)
    headers = {h["key"]: h["value"] for h in upload.get("headers", []) if "key" in h}
    try:
        data = _read_file(path)
    except OSError:
        return _fail("invalid", "attachment not found", retryable=False)
    try:
        resp = requests.put(upload_url, data=data, headers=headers, timeout=_HTTP_TIMEOUT)
    except (requests.ConnectionError, requests.Timeout):
        return _fail("network", "you appear to be offline", retryable=True)
    except requests.RequestException:
        return _fail("network", "upload failed", retryable=True)
    if resp.status_code >= 400:
        return _fail("server", "upload rejected", retryable=True)
    return None


def send_feedback(payload: dict) -> dict:
    """Carry one submission through prepare → upload → submit.

    ``payload``: ``{type, message, email?, versions{app,daemon,macos},
    attachments:[{path, content_type}]}``. Returns the stdout envelope.
    """
    attachments = payload.get("attachments") or []
    resolved = _validate_attachments(attachments)
    if isinstance(resolved, dict):  # a fail envelope
        return resolved

    submit_attachments: list[dict] = []
    if resolved:
        manifest = [{"content_type": a["content_type"], "size": a["size"]} for a in resolved]
        prep = _relay_post("prepare", {"attachments": manifest})
        if not prep.get("ok"):
            return prep
        uploads = prep.get("uploads") or []
        if len(uploads) != len(resolved):
            return _fail("server", "unexpected response", retryable=True)
        for upload, att in zip(uploads, resolved):
            failure = _put_file(upload, att["path"])
            if failure is not None:
                return failure
            submit_attachments.append(
                {"assetUrl": upload.get("assetUrl", ""), "claim": upload.get("claim", "")}
            )

    submit_body = {
        "type": payload.get("type"),
        "message": payload.get("message"),
        "email": payload.get("email", ""),
        "versions": payload.get("versions") or {},
        "attachments": submit_attachments,
    }
    return _relay_post("submit", submit_body)
