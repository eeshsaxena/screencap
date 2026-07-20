"""Tests for the in-app feedback relay (feedback.py, U1).

Linear is fully mocked — no network. Covers routing, caps, rate limiting,
the HMAC claim lifecycle, injection-hardened rendering, the GraphQL
static-query/variables contract, and client-opaque error mapping.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import feedback
import flask
import pytest

HMAC_KEY = "test-hmac-key-not-a-real-secret"


@pytest.fixture(autouse=True)
def relay_env():
    with mock.patch.dict(
        os.environ,
        {
            "FEEDBACK_HMAC_KEY": HMAC_KEY,
            "LINEAR_API_KEY": "lin_api_test",
            "SCREENCAP_LINEAR_TEAM_ID": "team-123",
            "SCREENCAP_LINEAR_TRIAGE_STATE_ID": "state-triage",
            "SCREENCAP_LINEAR_LABEL_SOURCE": "label-source",
            "SCREENCAP_LINEAR_LABEL_BUG": "label-bug",
            "SCREENCAP_LINEAR_LABEL_FEEDBACK": "label-feedback",
            "SCREENCAP_LINEAR_LABEL_FEATURE": "label-feature",
        },
        clear=False,
    ):
        yield


@pytest.fixture(autouse=True)
def app_context():
    app = flask.Flask(__name__)
    with app.app_context():
        yield


@pytest.fixture(autouse=True)
def reset_rate_state():
    feedback._submit_hits.clear()
    feedback._prepare_hits.clear()
    feedback._declared_bytes.clear()
    yield


def _req(body, *, method="POST", content_type="application/json", xff="9.9.9.9"):
    raw = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    headers = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    return mock.Mock(
        method=method,
        headers=headers,
        remote_addr="127.0.0.1",
        get_data=mock.Mock(return_value=raw),
    )


def _invoke(body, **kwargs):
    resp = feedback.submit_feedback(_req(body, **kwargs))
    payload = resp.get_json()
    return resp, payload


class _FakeResp:
    def __init__(self, data=None, status_code=200, errors=None):
        self._data = data or {}
        self.status_code = status_code
        self._errors = errors

    def json(self):
        out = {"data": self._data}
        if self._errors is not None:
            out["errors"] = self._errors
        return out


def _upload_file(i):
    return {
        "fileUpload": {
            "success": True,
            "uploadFile": {
                "uploadUrl": f"https://uploads.linear.app/upload/{i}",
                "assetUrl": f"https://uploads.linear.app/asset/{i}.png",
                "headers": [{"key": "x-h", "value": "v"}],
            },
        }
    }


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------
def test_submit_text_only_creates_issue():
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return _FakeResp({"issueCreate": {"success": True, "issue": {"id": "i1", "url": "https://linear.app/x/i1"}}})

    with mock.patch("feedback.requests.post", side_effect=fake_post):
        resp, payload = _invoke({"action": "submit", "type": "bug", "message": "It crashed"})

    assert resp.status_code == 200
    assert payload["ok"] is True
    assert payload["issue_url"] == "https://linear.app/x/i1"
    variables = calls[0]["variables"]["input"]
    assert variables["teamId"] == "team-123"
    assert variables["stateId"] == "state-triage"
    # Source marker leads, then the per-type label.
    assert variables["labelIds"] == ["label-source", "label-bug"]
    assert variables["title"] == "[Bug] It crashed"
    assert "Report metadata" in variables["description"]


def test_source_marker_label_applied_to_every_type():
    # Every relay-created issue carries the in-app-feedback source marker
    # regardless of request type, so maintainers can filter user submissions.
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["input"] = json["variables"]["input"]
        return _FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "https://linear.app/x/i"}}})

    for req_type, type_label in (("feature", "label-feature"), ("feedback", "label-feedback")):
        with mock.patch("feedback.requests.post", side_effect=fake_post):
            _invoke({"action": "submit", "type": req_type, "message": "hi"})
        assert seen["input"]["labelIds"] == ["label-source", type_label], req_type


def test_prepare_then_submit_embeds_both_attachments():
    posted = {"count": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        if "fileUpload" in json["query"]:
            posted["count"] += 1
            return _FakeResp(_upload_file(posted["count"]))
        return _FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "https://linear.app/x/i"}}})

    manifest = [
        {"filename": "a.png", "content_type": "image/png", "size": 1000},
        {"filename": "b.mp4", "content_type": "video/mp4", "size": 2000},
    ]
    with mock.patch("feedback.requests.post", side_effect=fake_post):
        _, prep = _invoke({"action": "prepare", "attachments": manifest})
        assert len(prep["uploads"]) == 2
        attachments = [{"assetUrl": u["assetUrl"], "claim": u["claim"]} for u in prep["uploads"]]
        resp, sub = _invoke(
            {"action": "submit", "type": "feedback", "message": "nice", "attachments": attachments}
        )

    assert sub["ok"] is True
    # Both asset urls embed as markdown images in the assembled description.
    desc = feedback.build_description(
        {"message": "nice", "email": "", "versions": {"app": "1", "daemon": "1", "macos": "1"}},
        [a["assetUrl"] for a in attachments],
    )
    assert "![attachment-1](https://uploads.linear.app/asset/1.png)" in desc
    assert "![attachment-2](https://uploads.linear.app/asset/2.png)" in desc


# --------------------------------------------------------------------------
# Caps (AE2 / AE6 / AE7 server side)
# --------------------------------------------------------------------------
def test_prepare_rejects_oversize_file():
    _, payload = _invoke(
        {"action": "prepare", "attachments": [{"filename": "big.mp4", "content_type": "video/mp4", "size": 30 * 1024 * 1024}]}
    )
    assert payload["error_kind"] == "too_large"


def test_prepare_rejects_total_cap():
    files = [{"filename": f"{i}.png", "content_type": "image/png", "size": 20 * 1024 * 1024} for i in range(4)]
    _, payload = _invoke({"action": "prepare", "attachments": files})
    assert payload["error_kind"] == "too_large"


def test_prepare_rejects_sixth_attachment_as_invalid():
    files = [{"filename": f"{i}.png", "content_type": "image/png", "size": 10} for i in range(6)]
    _, payload = _invoke({"action": "prepare", "attachments": files})
    assert payload["error_kind"] == "invalid"


def test_prepare_rejects_disallowed_content_type():
    _, payload = _invoke(
        {"action": "prepare", "attachments": [{"filename": "x.txt", "content_type": "text/plain", "size": 10}]}
    )
    assert payload["error_kind"] == "invalid"


def test_prepare_gcs_pathstyle_upload_url_passes_but_foreign_bucket_fails():
    # U0 (SCR-281): the real workspace signs PUT URLs as
    # storage.googleapis.com/uploads.linear.app/<path>. Any other GCS bucket
    # must not be handed to the client as an upload target.
    def fake_post_for(upload_url):
        def fake_post(url, json=None, headers=None, timeout=None):
            body = _upload_file(1)
            body["fileUpload"]["uploadFile"]["uploadUrl"] = upload_url
            return _FakeResp(body)

        return fake_post

    manifest = [{"filename": "a.png", "content_type": "image/png", "size": 1000}]
    ok_url = "https://storage.googleapis.com/uploads.linear.app/ws/att/1?X-Goog-Expires=60"
    with mock.patch("feedback.requests.post", side_effect=fake_post_for(ok_url)):
        _, payload = _invoke({"action": "prepare", "attachments": manifest})
    assert payload["ok"] is True
    assert payload["uploads"][0]["uploadUrl"] == ok_url

    # Foreign bucket as first path segment, and a dot-segment an RFC-3986
    # client would collapse to another bucket after a raw prefix check passed.
    for evil_url in (
        "https://storage.googleapis.com/attacker-bucket/uploads.linear.app/1",
        "https://storage.googleapis.com/uploads.linear.app/../attacker-bucket/1",
    ):
        with mock.patch("feedback.requests.post", side_effect=fake_post_for(evil_url)):
            _, payload = _invoke({"action": "prepare", "attachments": manifest})
        assert payload["ok"] is False, evil_url


def test_submit_keeps_asseturl_on_the_narrow_host_allowlist():
    # assetUrl stays uploads.linear.app-only (_host_allowed) even though
    # uploadUrl now accepts the wider GCS path-style form. A GCS path-style
    # assetUrl — correctly bucket-pinned, so it would pass the LOOSER
    # _upload_target_allowed — must still be rejected at submit, guarding
    # against a future swap of the two guards in _handle_submit.
    gcs_asset = "https://storage.googleapis.com/uploads.linear.app/ws/att/1"
    claim = feedback.mint_claim(gcs_asset, exp=9_999_999_999)
    _, payload = _invoke(
        {
            "action": "submit",
            "type": "bug",
            "message": "hi",
            "attachments": [{"assetUrl": gcs_asset, "claim": claim}],
        }
    )
    assert payload["ok"] is False
    assert payload["error_kind"] == "invalid"


# --------------------------------------------------------------------------
# Transport guard (KTD-6)
# --------------------------------------------------------------------------
def test_non_json_content_type_rejected():
    resp, payload = _invoke({"action": "submit"}, content_type="text/plain")
    assert resp.status_code == 415
    assert payload["error_kind"] == "invalid"


def test_oversize_body_rejected_before_parse():
    big = b'{"action":"submit","message":"' + b"x" * (65 * 1024) + b'"}'
    resp, payload = _invoke(big)
    assert resp.status_code == 413
    assert payload["error_kind"] == "too_large"


def test_no_cors_header_emitted():
    resp, _ = _invoke({"action": "bogus"})
    assert "Access-Control-Allow-Origin" not in resp.headers


# --------------------------------------------------------------------------
# Rate limiting (AE8 / KTD-6)
# --------------------------------------------------------------------------
def test_submit_rate_limited_after_threshold():
    with mock.patch(
        "feedback.requests.post",
        return_value=_FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "u"}}}),
    ):
        for _ in range(feedback.MAX_SUBMITS_PER_WINDOW):
            _, ok = _invoke({"action": "submit", "type": "bug", "message": "hi"}, xff="1.1.1.1")
            assert ok["ok"] is True
        resp, blocked = _invoke({"action": "submit", "type": "bug", "message": "hi"}, xff="1.1.1.1")
    assert resp.status_code == 429
    assert blocked["error_kind"] == "rate_limited"
    # A different IP is unaffected.
    with mock.patch(
        "feedback.requests.post",
        return_value=_FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "u"}}}),
    ):
        _, other = _invoke({"action": "submit", "type": "bug", "message": "hi"}, xff="2.2.2.2")
    assert other["ok"] is True


def test_rightmost_xff_is_the_client_ip():
    # Spoofed leading entries must not mint a fresh budget; the true peer is last.
    with mock.patch(
        "feedback.requests.post",
        return_value=_FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "u"}}}),
    ):
        for _ in range(feedback.MAX_SUBMITS_PER_WINDOW):
            _invoke({"action": "submit", "type": "bug", "message": "hi"}, xff="1.2.3.4, 5.6.7.8")
        resp, blocked = _invoke(
            {"action": "submit", "type": "bug", "message": "hi"}, xff="9.9.9.9, 5.6.7.8"
        )
    assert blocked["error_kind"] == "rate_limited"


def test_declared_bytes_budget_rejects():
    manifest = [{"filename": "a.mp4", "content_type": "video/mp4", "size": 25 * 1024 * 1024}]

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResp(_upload_file(1))

    with mock.patch("feedback.requests.post", side_effect=fake_post):
        # 5 prepares * 25MB = 125MB > 120MB budget; the prepare count cap is also
        # 5, so exhaust with single-file prepares and confirm a byte-driven 429.
        results = [
            _invoke({"action": "prepare", "attachments": manifest}, xff="7.7.7.7")[1]
            for _ in range(feedback.MAX_PREPARES_PER_WINDOW)
        ]
    assert any(r.get("error_kind") == "rate_limited" for r in results)


# --------------------------------------------------------------------------
# Claim lifecycle (KTD-3)
# --------------------------------------------------------------------------
def test_expired_claim_returns_expired_not_invalid():
    url = "https://uploads.linear.app/asset/1.png"
    claim = feedback.mint_claim(url, exp=1)  # far in the past
    with pytest.raises(feedback.FeedbackError) as exc:
        feedback.verify_claim(url, claim, now=1_000_000)
    assert exc.value.kind == "expired"


def test_tampered_claim_returns_invalid():
    url = "https://uploads.linear.app/asset/1.png"
    claim = feedback.mint_claim(url, exp=9_999_999_999)
    tampered = "0" * 64 + "." + claim.split(".")[1]
    with pytest.raises(feedback.FeedbackError) as exc:
        feedback.verify_claim(url, tampered)
    assert exc.value.kind == "invalid"


def test_submit_rejects_foreign_host_asseturl():
    _, payload = _invoke(
        {
            "action": "submit",
            "type": "bug",
            "message": "hi",
            "attachments": [{"assetUrl": "https://evil.example/x.png", "claim": "x.1"}],
        }
    )
    assert payload["error_kind"] == "invalid"


# --------------------------------------------------------------------------
# GraphQL contract (KTD-4)
# --------------------------------------------------------------------------
def test_graphql_uses_static_query_and_variables_only():
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["query"] = json["query"]
        captured["variables"] = json["variables"]
        return _FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "u"}}})

    hostile = 'oops" } mutation{ evil ${x}'
    with mock.patch("feedback.requests.post", side_effect=fake_post):
        _invoke({"action": "submit", "type": "bug", "message": hostile})

    # Query document is byte-identical to the module constant.
    assert captured["query"] == feedback._ISSUE_CREATE_QUERY
    # The hostile string never appears in the query, only under variables.
    assert hostile not in captured["query"]
    assert hostile in captured["variables"]["input"]["description"]


# --------------------------------------------------------------------------
# Injection-hardened rendering (KTD-11)
# --------------------------------------------------------------------------
def test_message_markup_stays_inside_fence():
    msg = "line1\n```\n![x](http://evil)\nApp version: 9.9.9"
    desc = feedback.build_description(
        {"message": msg, "email": "", "versions": {"app": "1.0", "daemon": "1.0", "macos": "15.0"}},
        [],
    )
    # The server metadata block is below the delimiter, generated by us.
    header_idx = desc.index("**Report metadata**")
    # The hostile markup appears only before the metadata block, inside the fence.
    assert desc.index("![x](http://evil)") < header_idx


def test_email_with_markdown_breakout_rejected():
    # `x@y.z`![img](http://evil)` must NOT pass validation — it would close the
    # inline-code span and auto-load an image in the maintainer's triage.
    _, payload = _invoke(
        {
            "action": "submit",
            "type": "bug",
            "message": "hi",
            "email": "x@y.z`![img](http://evil)",
        }
    )
    assert payload["error_kind"] == "invalid"


def test_plain_email_accepted():
    with mock.patch(
        "feedback.requests.post",
        return_value=_FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "u"}}}),
    ):
        _, payload = _invoke(
            {"action": "submit", "type": "bug", "message": "hi", "email": "user.name+tag@example.co"}
        )
    assert payload["ok"] is True


def test_version_field_with_markup_rejected():
    _, payload = _invoke(
        {
            "action": "submit",
            "type": "bug",
            "message": "hi",
            "versions": {"app": "![x](http://evil)", "daemon": "1", "macos": "1"},
        }
    )
    assert payload["error_kind"] == "invalid"


def test_client_filename_never_in_description():
    def fake_post(url, json=None, headers=None, timeout=None):
        if "fileUpload" in json["query"]:
            return _FakeResp(_upload_file(1))
        return _FakeResp({"issueCreate": {"success": True, "issue": {"id": "i", "url": "u"}}})

    manifest = [{"filename": "](http://evil)", "content_type": "image/png", "size": 100}]
    with mock.patch("feedback.requests.post", side_effect=fake_post) as post:
        _, prep = _invoke({"action": "prepare", "attachments": manifest})
        att = [{"assetUrl": prep["uploads"][0]["assetUrl"], "claim": prep["uploads"][0]["claim"]}]
        _invoke({"action": "submit", "type": "bug", "message": "hi", "attachments": att})
        # The fileUpload variables carry a server-generated filename, not the client's.
        upload_call = [c for c in post.call_args_list if "fileUpload" in c.kwargs["json"]["query"]][0]
        assert upload_call.kwargs["json"]["variables"]["filename"] == "attachment-1"


# --------------------------------------------------------------------------
# Error mapping — client body carries no upstream text
# --------------------------------------------------------------------------
def test_linear_error_maps_to_opaque_server():
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResp(status_code=500, errors=[{"message": "SECRET upstream detail"}])

    with mock.patch("feedback.requests.post", side_effect=fake_post):
        resp, payload = _invoke({"action": "submit", "type": "bug", "message": "hi"})
    assert payload["error_kind"] == "server"
    assert "SECRET" not in json.dumps(payload)


def test_validation_errors_name_the_problem():
    _, missing_msg = _invoke({"action": "submit", "type": "bug", "message": "  "})
    assert missing_msg["error_kind"] == "invalid"
    _, bad_type = _invoke({"action": "submit", "type": "nope", "message": "hi"})
    assert bad_type["error_kind"] == "invalid"
    _, bad_email = _invoke({"action": "submit", "type": "bug", "message": "hi", "email": "notanemail"})
    assert bad_email["error_kind"] == "invalid"
