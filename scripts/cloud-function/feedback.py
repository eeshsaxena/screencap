"""In-app feedback relay Cloud Function (feat/in-app-feedback-form, U1).

A single tokenless HTTP function that carries a user's bug report / feedback /
feature request into the maintainer's Linear triage. It is the product's FIRST
unauthenticated endpoint — see SECURITY.md for the full trust boundary. What a
forged request can do is bounded to: create one triage issue and park bounded
bytes on Linear storage; it can read nothing.

Routing is on an ``action`` field:

- ``prepare`` — validate caps/types + rate/byte budgets, mint a Linear
  ``fileUpload`` signed URL per attachment, and return per file
  ``{uploadUrl, headers, assetUrl, claim}``. ``claim`` is a stateless HMAC over
  ``{assetUrl, exp}`` (KTD-3): the client echoes it back at ``submit`` so the
  relay never trusts a client-chosen URL. Bytes never pass through the function
  (mirrors main.py's signed-URL pattern), sidestepping the 32 MiB gen2 request
  ceiling.
- ``submit`` — validate fields (type enum, message, optional email, version
  strings), verify every assetUrl's claim + host allowlist, assemble an
  injection-hardened issue description, and ``issueCreate`` with a STATIC query
  document (user strings only in GraphQL ``variables``, KTD-4).

Security posture (SECURITY.md is the source of truth):
- Requires ``Content-Type: application/json`` and emits NO CORS headers, so a
  browser cannot deliver a forged cross-origin submission (KTD-6).
- In-process per-IP request + declared-bytes budgets, keyed on the RIGHTMOST
  ``X-Forwarded-For`` entry (Google appends the true peer). Best-effort /
  per-instance-lifetime — a spam speed-bump, not a hard quota — accepted only
  because deploy pins ``--max-instances=2``.
- ``LINEAR_API_KEY`` belongs to a dedicated least-privilege Linear account and
  never reaches the client or logs; ``FEEDBACK_HMAC_KEY`` is a separate secret.

Deploy (project: proteus-photos, region: southamerica-east1):
    # This entry point lives in feedback.py, but the GCF Python buildpack DEFAULTS
    # the source to main.py — so the deploy MUST pass
    # --set-build-env-vars GOOGLE_FUNCTION_SOURCE=feedback.py or the container
    # fails at startup with MissingTargetException. Runs as a dedicated
    # least-privilege SA holding only Secret Manager accessor on its two secrets:
    #   gcloud iam service-accounts create screencap-feedback --project proteus-photos
    #   for SECRET in LINEAR_API_KEY FEEDBACK_HMAC_KEY; do
    #     gcloud secrets add-iam-policy-binding "$SECRET" --project proteus-photos \
    #       --member serviceAccount:screencap-feedback@proteus-photos.iam.gserviceaccount.com \
    #       --role roles/secretmanager.secretAccessor
    #   done
    #
    # Generate the HMAC secret with real entropy (a weak value makes claims
    # offline-forgeable):  openssl rand -base64 32 | gcloud secrets create FEEDBACK_HMAC_KEY --data-file=-
    #
    # Routing ids resolved against the real Screencap team (SCR-281 U0). Issues
    # land in Backlog and always carry the in-app-feedback source marker
    # (SCREENCAP_LINEAR_LABEL_SOURCE) plus their per-type label. NOTE: the team
    # has no "Triage" workflow state, so TRIAGE_STATE_ID points at Backlog.
    #   SCREENCAP_LINEAR_TEAM_ID         = f3bbac41-0ec3-4f2e-ae0b-96fed6ff624f  (Screencap)
    #   SCREENCAP_LINEAR_TRIAGE_STATE_ID = c825fa67-c31e-421c-a5f9-ab2aef7ef5f5  (Backlog)
    #   SCREENCAP_LINEAR_LABEL_SOURCE    = b0a3860f-12cd-4831-ade2-a787f46729a3  (in-app-feedback)
    #   SCREENCAP_LINEAR_LABEL_BUG       = fc126e8e-a621-4acb-b326-05f1c047dbf4  (Bug)
    #   SCREENCAP_LINEAR_LABEL_FEEDBACK  = 5307398b-3a47-462b-be99-63a38b941001  (Feedback)
    #   SCREENCAP_LINEAR_LABEL_FEATURE   = 0948a8cf-bb9b-4df4-a190-96b70ea3e905  (Feature)
    #
    gcloud functions deploy submit-feedback \
        --project proteus-photos --gen2 --runtime python312 \
        --trigger-http --allow-unauthenticated \
        --region southamerica-east1 --source scripts/cloud-function/ \
        --entry-point submit_feedback \
        --set-build-env-vars GOOGLE_FUNCTION_SOURCE=feedback.py \
        --service-account screencap-feedback@proteus-photos.iam.gserviceaccount.com \
        --max-instances 2 --memory 256Mi \
        --set-secrets LINEAR_API_KEY=LINEAR_API_KEY:latest,FEEDBACK_HMAC_KEY=FEEDBACK_HMAC_KEY:latest \
        --set-env-vars SCREENCAP_LINEAR_TEAM_ID=f3bbac41-0ec3-4f2e-ae0b-96fed6ff624f,SCREENCAP_LINEAR_TRIAGE_STATE_ID=c825fa67-c31e-421c-a5f9-ab2aef7ef5f5,SCREENCAP_LINEAR_LABEL_SOURCE=b0a3860f-12cd-4831-ade2-a787f46729a3,SCREENCAP_LINEAR_LABEL_BUG=fc126e8e-a621-4acb-b326-05f1c047dbf4,SCREENCAP_LINEAR_LABEL_FEEDBACK=5307398b-3a47-462b-be99-63a38b941001,SCREENCAP_LINEAR_LABEL_FEATURE=0948a8cf-bb9b-4df4-a190-96b70ea3e905

    # Runbook: rotate LINEAR_API_KEY via Linear settings + redeploy; rotating
    # FEEDBACK_HMAC_KEY voids unexpired claims (clients simply re-prepare).
    # Watch Linear storage usage — orphaned prepare-without-submit uploads are
    # the expected abuse signature.

U0 spike results (SCR-281, run 2026-07-20 against the real workspace): 25 MiB
``.mov``, ``.png`` and ``.heic`` uploads all accepted, so the caps stand;
assetUrls live on ``uploads.linear.app`` but signed PUT URLs are GCS
path-style (``storage.googleapis.com/uploads.linear.app/...``) — see
``_upload_target_allowed``; the signed upload URL expires 60 s after minting,
which set ``CLAIM_TTL_SECONDS`` below. Constants stay mirrored in
``src/screencap/feedback.py`` (the CLI↔relay equality test pins them).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import functions_framework
import requests
from flask import Response, jsonify

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Mirrored constants (KTD-5) — kept EQUAL to src/screencap/feedback.py. No
# import mechanism spans the deployed function and the CLI, so the CLI↔relay
# equality test (tests/test_feedback.py is Swift-free; the pytest in
# test_feedback.py here + the CLI test pin these) guards drift.
# --------------------------------------------------------------------------
MAX_FILE_BYTES = 25 * 1024 * 1024          # 25 MB per file
MAX_TOTAL_BYTES = 60 * 1024 * 1024         # 60 MB total across attachments
MAX_ATTACHMENTS = 5

# Accepted attachment content types (images + short video).
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

# Hosts an attachment assetUrl may point at. Confirmed by the U0 spike
# (SCR-281) against the real workspace: assetUrls are served from
# uploads.linear.app.
LINEAR_UPLOAD_HOSTS = frozenset({"uploads.linear.app"})

# U0 finding (SCR-281): the real workspace signs PUT URLs in GCS path style —
# https://storage.googleapis.com/uploads.linear.app/<path> — so the upload
# target is allowed EITHER on the Linear host or on GCS pinned to Linear's
# bucket path. assetUrls stay uploads.linear.app-only (LINEAR_UPLOAD_HOSTS).
LINEAR_UPLOAD_GCS_HOST = "storage.googleapis.com"
LINEAR_UPLOAD_GCS_PREFIX = "/uploads.linear.app/"

# Request-type enum -> the env var naming its Linear label id.
REQUEST_TYPES = {
    "bug": "SCREENCAP_LINEAR_LABEL_BUG",
    "feedback": "SCREENCAP_LINEAR_LABEL_FEEDBACK",
    "feature": "SCREENCAP_LINEAR_LABEL_FEATURE",
}

MAX_MESSAGE_CHARS = 10_000
MAX_BODY_BYTES = 64 * 1024                  # JSON control plane only; no file bytes
# U0 finding (SCR-281): Linear's signed upload URL must be USED within 60 s of
# minting (X-Goog-Expires=60; GCS checks the signature when the PUT request
# arrives, so an already-started slow stream may finish). A 60-min claim would
# outlive that 60 s upload window by an hour for no benefit; 10 min bounds the
# HMAC-claim replay window while still covering a client's prepare→submit round
# trip. It does NOT rescue a slow multi-file upload — the signed URLs, not the
# claim, are the 60 s constraint (see SCR-285: prepare batch-mints all URLs but
# the CLI PUTs sequentially). ``expired`` stays retryable — the client
# re-prepares.
CLAIM_TTL_SECONDS = 10 * 60

# Per-IP budgets (KTD-6). Best-effort per-instance-lifetime.
RATE_WINDOW_SECONDS = 60 * 60
MAX_SUBMITS_PER_WINDOW = 5
MAX_PREPARES_PER_WINDOW = 5
MAX_DECLARED_BYTES_PER_WINDOW = 120 * 1024 * 1024

# Metadata version fields render OUTSIDE the fenced message, so they are the same
# injection surface as message/email and are validated to this pattern (KTD-11).
_VERSION_RE = re.compile(r"^[A-Za-z0-9 ().\-]{1,64}$")
# Conservative charset: excludes every markdown-breakout character (backtick,
# brackets, parens, bang) so a validated email cannot escape the inline-code
# span it renders into (KTD-11). A permissive "anything but @/space" pattern
# would let `x@y.z`![img](http://evil)` break out and auto-load an image in the
# maintainer's triage.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
# Connect/read timeouts mirror upload.py's tuple.
_HTTP_TIMEOUT = (10, 300)

# In-process rate state: ip -> list[timestamp] (submits, prepares) and
# ip -> list[(timestamp, declared_bytes)]. Reset on cold start (accepted, KTD-6).
_submit_hits: dict[str, list[float]] = {}
_prepare_hits: dict[str, list[float]] = {}
_declared_bytes: dict[str, list[tuple[float, int]]] = {}


class FeedbackError(Exception):
    """A typed, client-safe failure. ``kind`` is one of the taxonomy values."""

    def __init__(self, kind: str, message: str, status: int):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


def _err(kind: str, message: str, status: int) -> FeedbackError:
    return FeedbackError(kind, message, status)


# --------------------------------------------------------------------------
# HMAC claims (KTD-3)
# --------------------------------------------------------------------------
def _hmac_key() -> bytes:
    key = os.environ.get("FEEDBACK_HMAC_KEY", "")
    if not key:
        # Fail closed: without a key we cannot mint or verify claims safely.
        raise _err("server", "relay misconfigured", 500)
    return key.encode("utf-8")


def mint_claim(asset_url: str, exp: int) -> str:
    """Return ``"<b64 mac>.<exp>"`` binding ``asset_url`` to an expiry."""
    payload = f"{asset_url}\n{exp}".encode("utf-8")
    mac = hmac.new(_hmac_key(), payload, hashlib.sha256).hexdigest()
    return f"{mac}.{exp}"


def verify_claim(asset_url: str, claim: str, now: float | None = None) -> None:
    """Raise ``FeedbackError`` unless ``claim`` is authentic and unexpired.

    ``expired`` is a distinct kind from ``invalid`` so the client can tell an
    honest slow-upload timeout from a tampered/forged claim (KTD-7).
    """
    now = time.time() if now is None else now
    if not isinstance(claim, str) or claim.count(".") != 1:
        raise _err("invalid", "malformed claim", 400)
    mac_hex, _, exp_str = claim.partition(".")
    try:
        exp = int(exp_str)
    except ValueError as exc:
        raise _err("invalid", "malformed claim", 400) from exc
    expected = hmac.new(
        _hmac_key(), f"{asset_url}\n{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(mac_hex, expected):
        raise _err("invalid", "claim verification failed", 400)
    if now > exp:
        raise _err("expired", "upload authorization expired", 400)


def _host_allowed(url: str) -> bool:
    """True iff ``url`` is https and its exact host is on the allowlist."""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in LINEAR_UPLOAD_HOSTS


def _upload_target_allowed(url: str) -> bool:
    """True iff ``url`` may receive attachment bytes: https on a Linear upload
    host, or GCS path-style pinned to Linear's bucket (either way, bytes can
    only land in Linear-controlled storage)."""
    if _host_allowed(url):
        return True
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if not (parts.scheme == "https" and parts.hostname == LINEAR_UPLOAD_GCS_HOST):
        return False
    # Reject dot-segments before the prefix check: requests/urllib3 (and other
    # RFC 3986 clients) collapse "/../" BEFORE sending, so a path that passes a
    # raw prefix check (".../uploads.linear.app/../other-bucket/x") is rewritten
    # to a DIFFERENT bucket by the time bytes go out. Linear's real signed URLs
    # are all UUID path segments with no dot-segments, so this only rejects
    # forged targets.
    if any(seg in ("..", ".") for seg in parts.path.split("/")):
        return False
    return parts.path.startswith(LINEAR_UPLOAD_GCS_PREFIX)


# --------------------------------------------------------------------------
# Rate limiting (KTD-6)
# --------------------------------------------------------------------------
def client_ip(request) -> str:
    """The RIGHTMOST X-Forwarded-For entry (Google appends the true peer).

    Reading the first entry would let an attacker mint fresh budgets with
    spoofed leading entries.
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.remote_addr or "unknown"


def _prune(hits: list[float], now: float) -> list[float]:
    cutoff = now - RATE_WINDOW_SECONDS
    return [t for t in hits if t >= cutoff]


def _enforce_count(store: dict[str, list[float]], ip: str, limit: int, now: float) -> None:
    hits = _prune(store.get(ip, []), now)
    if len(hits) >= limit:
        store[ip] = hits
        raise _err("rate_limited", "too many reports, try again later", 429)
    hits.append(now)
    store[ip] = hits


def _sweep_stale(now: float) -> None:
    """Drop rate-limit keys whose entries are all older than the window.

    Prune-on-touch alone never removes IP *keys*, so a long-lived warm instance
    would accumulate keys under organic traffic. This bounds the three dicts to
    IPs seen within the window (the rightmost XFF is Google-set, so keys can't be
    inflated with spoofed values). Cheap at this scale — keys ~ distinct IPs/hr.
    """
    cutoff = now - RATE_WINDOW_SECONDS
    for store in (_submit_hits, _prepare_hits):
        for ip in [k for k, v in store.items() if not any(t >= cutoff for t in v)]:
            del store[ip]
    for ip in [
        k for k, v in _declared_bytes.items() if not any(t >= cutoff for t, _ in v)
    ]:
        del _declared_bytes[ip]


def _enforce_declared_bytes(ip: str, add_bytes: int, now: float) -> None:
    cutoff = now - RATE_WINDOW_SECONDS
    entries = [(t, b) for (t, b) in _declared_bytes.get(ip, []) if t >= cutoff]
    used = sum(b for _, b in entries)
    if used + add_bytes > MAX_DECLARED_BYTES_PER_WINDOW:
        _declared_bytes[ip] = entries
        raise _err("rate_limited", "too many reports, try again later", 429)
    entries.append((now, add_bytes))
    _declared_bytes[ip] = entries


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def validate_manifest(manifest) -> list[dict]:
    """Validate an attachment manifest for ``prepare``; return the clean list.

    Each entry needs ``filename`` (str), ``content_type`` (allowlisted), and
    ``size`` (int within the per-file cap). Enforces count and total caps.
    """
    if not isinstance(manifest, list):
        raise _err("invalid", "attachments must be a list", 400)
    if len(manifest) > MAX_ATTACHMENTS:
        raise _err("invalid", f"at most {MAX_ATTACHMENTS} attachments", 400)
    total = 0
    clean: list[dict] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            raise _err("invalid", "malformed attachment", 400)
        content_type = entry.get("content_type")
        size = entry.get("size")
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise _err("invalid", "unsupported attachment type", 400)
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise _err("invalid", "invalid attachment size", 400)
        if size > MAX_FILE_BYTES:
            raise _err("too_large", "attachment over the size limit", 400)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise _err("too_large", "attachments over the total size limit", 400)
        clean.append({"content_type": content_type, "size": size})
    return clean


def _validate_submit_fields(body: dict) -> dict:
    request_type = body.get("type")
    if request_type not in REQUEST_TYPES:
        raise _err("invalid", "unknown request type", 400)
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _err("invalid", "message is required", 400)
    if len(message) > MAX_MESSAGE_CHARS:
        raise _err("invalid", "message is too long", 400)
    email = body.get("email")
    if email is not None and email != "":
        if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email):
            raise _err("invalid", "invalid email", 400)
    versions = body.get("versions") or {}
    if not isinstance(versions, dict):
        raise _err("invalid", "invalid metadata", 400)
    clean_versions: dict[str, str] = {}
    for field in ("app", "daemon", "macos"):
        value = versions.get(field, "unavailable")
        if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
            raise _err("invalid", f"invalid {field} version", 400)
        clean_versions[field] = value
    return {
        "type": request_type,
        "message": message,
        "email": email or "",
        "versions": clean_versions,
    }


# --------------------------------------------------------------------------
# Injection-hardened issue description (KTD-11)
# --------------------------------------------------------------------------
def _fence(message: str) -> str:
    """Render user text inside a fenced code block, escaping backtick runs.

    A fence longer than any run in the content prevents an early close-out.
    """
    longest = 0
    run = 0
    for ch in message:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{message}\n{fence}"


def build_description(fields: dict, asset_urls: list[str]) -> str:
    """Assemble the Linear issue description body.

    The user message is fenced; the metadata block is server-generated below a
    fixed delimiter so a fabricated in-message block is distinguishable; version
    strings and email render as inline code; attachments use server-generated
    labels (client filenames are discarded).
    """
    parts = [_fence(fields["message"]), "", "---", "**Report metadata**", ""]
    v = fields["versions"]
    email = fields["email"] or "not provided"
    parts.append(f"- App version: `{v['app']}`")
    parts.append(f"- Daemon version: `{v['daemon']}`")
    parts.append(f"- macOS version: `{v['macos']}`")
    parts.append(f"- Reply email: `{email}`")
    if asset_urls:
        parts.append("")
        parts.append("**Attachments**")
        parts.append("")
        for i, url in enumerate(asset_urls, start=1):
            parts.append(f"![attachment-{i}]({url})")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Linear API (KTD-4) — STATIC query documents; user strings only in variables.
# --------------------------------------------------------------------------
_FILE_UPLOAD_QUERY = """
mutation FileUpload($contentType: String!, $filename: String!, $size: Int!) {
  fileUpload(contentType: $contentType, filename: $filename, size: $size) {
    success
    uploadFile { uploadUrl assetUrl headers { key value } }
  }
}
""".strip()

_ISSUE_CREATE_QUERY = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id url }
  }
}
""".strip()


def _linear_headers() -> dict:
    key = os.environ.get("LINEAR_API_KEY", "")
    if not key:
        raise _err("server", "relay misconfigured", 500)
    return {"Authorization": key, "Content-Type": "application/json"}


def _linear_post(query: str, variables: dict) -> dict:
    """POST a GraphQL op; raise a client-opaque ``server`` error on any failure.

    Upstream response bodies are NEVER forwarded to the client.
    """
    try:
        resp = requests.post(
            LINEAR_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=_linear_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        logger.warning("linear request transport error: %s", type(exc).__name__)
        raise _err("server", "something went wrong", 502) from exc
    if resp.status_code >= 400:
        logger.warning("linear responded %s", resp.status_code)
        raise _err("server", "something went wrong", 502)
    payload = resp.json()
    if payload.get("errors"):
        logger.warning("linear graphql returned errors")
        raise _err("server", "something went wrong", 502)
    return payload.get("data") or {}


def _linear_file_upload(content_type: str, filename: str, size: int) -> dict:
    data = _linear_post(
        _FILE_UPLOAD_QUERY,
        {"contentType": content_type, "filename": filename, "size": size},
    )
    upload = (data.get("fileUpload") or {})
    if not upload.get("success"):
        raise _err("server", "something went wrong", 502)
    return upload.get("uploadFile") or {}


def _linear_create_issue(title: str, description: str, label_id: str) -> dict:
    team_id = os.environ.get("SCREENCAP_LINEAR_TEAM_ID", "")
    state_id = os.environ.get("SCREENCAP_LINEAR_TRIAGE_STATE_ID", "")
    if not team_id or not state_id:
        raise _err("server", "relay misconfigured", 500)
    issue_input = {
        "teamId": team_id,
        "stateId": state_id,
        "title": title,
        "description": description,
    }
    # Every relay-created issue carries the source marker label (so maintainers
    # can see at a glance the issue came from a user's in-app submission, not an
    # internally-filed ticket) plus its per-request-type label. Source first so
    # it reads as the primary tag.
    source_label = os.environ.get("SCREENCAP_LINEAR_LABEL_SOURCE", "")
    label_ids = [lid for lid in (source_label, label_id) if lid]
    if label_ids:
        issue_input["labelIds"] = label_ids
    data = _linear_post(_ISSUE_CREATE_QUERY, {"input": issue_input})
    created = data.get("issueCreate") or {}
    if not created.get("success"):
        raise _err("server", "something went wrong", 502)
    return created.get("issue") or {}


# --------------------------------------------------------------------------
# Action handlers
# --------------------------------------------------------------------------
def _handle_prepare(body: dict, ip: str, now: float) -> dict:
    manifest = validate_manifest(body.get("attachments") or [])
    _enforce_count(_prepare_hits, ip, MAX_PREPARES_PER_WINDOW, now)
    _enforce_declared_bytes(ip, sum(e["size"] for e in manifest), now)
    exp = int(now) + CLAIM_TTL_SECONDS
    uploads = []
    for i, entry in enumerate(manifest, start=1):
        # Server-generated filename; the client's is never trusted or rendered.
        filename = f"attachment-{i}"
        upload_file = _linear_file_upload(entry["content_type"], filename, entry["size"])
        asset_url = upload_file.get("assetUrl", "")
        upload_url = upload_file.get("uploadUrl", "")
        if not _host_allowed(asset_url) or not _upload_target_allowed(upload_url):
            # Linear returned an unexpected host — fail rather than mint a claim.
            raise _err("server", "something went wrong", 502)
        uploads.append(
            {
                "uploadUrl": upload_url,
                "assetUrl": asset_url,
                "headers": upload_file.get("headers") or [],
                "claim": mint_claim(asset_url, exp),
            }
        )
    return {"ok": True, "uploads": uploads}


def _handle_submit(body: dict, ip: str, now: float) -> dict:
    fields = _validate_submit_fields(body)
    _enforce_count(_submit_hits, ip, MAX_SUBMITS_PER_WINDOW, now)
    asset_urls: list[str] = []
    for att in body.get("attachments") or []:
        if not isinstance(att, dict):
            raise _err("invalid", "malformed attachment", 400)
        asset_url = att.get("assetUrl", "")
        claim = att.get("claim", "")
        if not _host_allowed(asset_url):
            raise _err("invalid", "attachment host not allowed", 400)
        verify_claim(asset_url, claim, now=now)
        asset_urls.append(asset_url)
    title = _issue_title(fields["type"], fields["message"])
    description = build_description(fields, asset_urls)
    label_id = os.environ.get(REQUEST_TYPES[fields["type"]], "")
    issue = _linear_create_issue(title, description, label_id)
    return {"ok": True, "issue_url": issue.get("url"), "issue_id": issue.get("id")}


def _issue_title(request_type: str, message: str) -> str:
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    label = {"bug": "Bug", "feedback": "Feedback", "feature": "Feature"}[request_type]
    return f"[{label}] {first_line}".strip()


# --------------------------------------------------------------------------
# HTTP entry point
# --------------------------------------------------------------------------
def _json_error(kind: str, message: str, status: int) -> Response:
    # NO CORS headers (KTD-6): browsers must not be able to deliver this request.
    resp = jsonify({"ok": False, "error_kind": kind, "message": message})
    resp.status_code = status
    return resp


@functions_framework.http
def submit_feedback(request):
    """Route a feedback submission on the ``action`` field."""
    if request.method != "POST":
        return _json_error("invalid", "POST required", 405)
    # Browser-CSRF guard (KTD-6): require JSON, forcing a CORS preflight browsers
    # cannot satisfy (we emit no Access-Control-Allow-Origin), and reject before
    # reading the body.
    content_type = (request.headers.get("Content-Type") or "").split(";")[0].strip()
    if content_type != "application/json":
        return _json_error("invalid", "application/json required", 415)
    raw = request.get_data(cache=False, as_text=False) or b""
    if len(raw) > MAX_BODY_BYTES:
        return _json_error("too_large", "request too large", 413)
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return _json_error("invalid", "invalid JSON", 400)
    if not isinstance(body, dict):
        return _json_error("invalid", "invalid payload", 400)

    ip = client_ip(request)
    now = time.time()
    _sweep_stale(now)
    action = body.get("action")
    try:
        if action == "prepare":
            result = _handle_prepare(body, ip, now)
        elif action == "submit":
            result = _handle_submit(body, ip, now)
        else:
            raise _err("invalid", "unknown action", 400)
    except FeedbackError as exc:
        return _json_error(exc.kind, exc.message, exc.status)
    except Exception:  # noqa: BLE001 — never leak internals to the client
        logger.exception("unexpected feedback relay error")
        return _json_error("server", "something went wrong", 500)
    return jsonify(result)
