"""Tests for the feedback CLI relay client (src/screencap/feedback.py, U2)."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest import mock

import pytest

from screencap import feedback


class _Resp:
    def __init__(self, payload=None, status_code=200, raise_exc=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self._raise = raise_exc

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _png(tmp_path: Path, name="a.png", size=100) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------
def test_text_only_skips_prepare_and_put(tmp_path):
    posts = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json["action"])
        return _Resp({"ok": True, "issue_url": "https://linear.app/x/i", "issue_id": "i"})

    with mock.patch("screencap.feedback.requests.post", side_effect=fake_post), \
        mock.patch("screencap.feedback.requests.put") as put:
        env = feedback.send_feedback({"type": "bug", "message": "it broke"})

    assert env["ok"] is True
    assert env["issue_url"] == "https://linear.app/x/i"
    assert posts == ["submit"]          # no prepare
    put.assert_not_called()             # no upload


def test_with_attachments_prepares_puts_submits(tmp_path):
    f1, f2 = _png(tmp_path, "a.png"), _png(tmp_path, "b.png")
    prep = {
        "ok": True,
        "uploads": [
            {"uploadUrl": "https://uploads.linear.app/up/1", "assetUrl": "https://uploads.linear.app/a/1", "claim": "c1", "headers": [{"key": "h", "value": "v"}]},
            {"uploadUrl": "https://uploads.linear.app/up/2", "assetUrl": "https://uploads.linear.app/a/2", "claim": "c2", "headers": []},
        ],
    }
    submit_bodies = []

    def fake_post(url, json=None, headers=None, timeout=None):
        if json["action"] == "prepare":
            return _Resp(prep)
        submit_bodies.append(json)
        return _Resp({"ok": True, "issue_url": "u", "issue_id": "i"})

    with mock.patch("screencap.feedback.requests.post", side_effect=fake_post), \
        mock.patch("screencap.feedback.requests.put", return_value=_Resp(status_code=200)) as put:
        env = feedback.send_feedback(
            {
                "type": "feedback",
                "message": "hi",
                "attachments": [
                    {"path": str(f1), "content_type": "image/png"},
                    {"path": str(f2), "content_type": "image/png"},
                ],
            }
        )

    assert env["ok"] is True
    assert put.call_count == 2
    assert submit_bodies[0]["attachments"] == [
        {"assetUrl": "https://uploads.linear.app/a/1", "claim": "c1"},
        {"assetUrl": "https://uploads.linear.app/a/2", "claim": "c2"},
    ]


# --------------------------------------------------------------------------
# Error taxonomy
# --------------------------------------------------------------------------
def test_offline_submit_maps_to_network_retryable():
    with mock.patch("screencap.feedback.requests.post", side_effect=feedback.requests.ConnectionError()):
        env = feedback.send_feedback({"type": "bug", "message": "hi"})
    assert env == {"ok": False, "error_kind": "network", "message": mock.ANY, "retryable": True}


def test_relay_rate_limited_passes_through_non_retryable():
    with mock.patch(
        "screencap.feedback.requests.post",
        return_value=_Resp({"ok": False, "error_kind": "rate_limited", "message": "later"}),
    ):
        env = feedback.send_feedback({"type": "bug", "message": "hi"})
    assert env["error_kind"] == "rate_limited"
    assert env["retryable"] is False


def test_relay_expired_is_retryable():
    with mock.patch(
        "screencap.feedback.requests.post",
        return_value=_Resp({"ok": False, "error_kind": "expired", "message": "timed out"}),
    ):
        env = feedback.send_feedback({"type": "bug", "message": "hi"})
    assert env["error_kind"] == "expired"
    assert env["retryable"] is True


def test_oversize_file_rejected_before_any_network(tmp_path):
    big = _png(tmp_path, "big.mp4", size=feedback.MAX_FILE_BYTES + 1)
    with mock.patch("screencap.feedback.requests.post") as post, \
        mock.patch("screencap.feedback.requests.put") as put:
        env = feedback.send_feedback(
            {"type": "bug", "message": "hi", "attachments": [{"path": str(big), "content_type": "video/mp4"}]}
        )
    assert env["error_kind"] == "too_large"
    post.assert_not_called()
    put.assert_not_called()


def test_failed_put_stops_before_submit(tmp_path):
    f1 = _png(tmp_path)
    prep = {"ok": True, "uploads": [{"uploadUrl": "https://uploads.linear.app/up/1", "assetUrl": "https://uploads.linear.app/a/1", "claim": "c1", "headers": []}]}
    actions = []

    def fake_post(url, json=None, headers=None, timeout=None):
        actions.append(json["action"])
        return _Resp(prep)

    with mock.patch("screencap.feedback.requests.post", side_effect=fake_post), \
        mock.patch("screencap.feedback.requests.put", return_value=_Resp(status_code=500)):
        env = feedback.send_feedback(
            {"type": "bug", "message": "hi", "attachments": [{"path": str(f1), "content_type": "image/png"}]}
        )
    assert env["ok"] is False
    assert env["error_kind"] == "server"
    assert actions == ["prepare"]       # submit never called


def test_non_allowlisted_upload_url_sends_no_bytes(tmp_path):
    f1 = _png(tmp_path)
    prep = {"ok": True, "uploads": [{"uploadUrl": "http://evil.example/up", "assetUrl": "https://uploads.linear.app/a/1", "claim": "c1", "headers": []}]}

    with mock.patch("screencap.feedback.requests.post", return_value=_Resp(prep)), \
        mock.patch("screencap.feedback.requests.put") as put:
        env = feedback.send_feedback(
            {"type": "bug", "message": "hi", "attachments": [{"path": str(f1), "content_type": "image/png"}]}
        )
    assert env["ok"] is False
    assert env["error_kind"] == "server"
    put.assert_not_called()             # no bytes left the machine


def test_gcs_pathstyle_upload_url_allowed_but_foreign_bucket_rejected(tmp_path):
    # U0 (SCR-281): the real workspace signs PUT URLs as
    # storage.googleapis.com/uploads.linear.app/<path>; any other GCS bucket
    # stays rejected.
    f1 = _png(tmp_path)

    def prep_for(upload_url):
        return {"ok": True, "uploads": [{"uploadUrl": upload_url, "assetUrl": "https://uploads.linear.app/a/1", "claim": "c1", "headers": []}]}

    ok_url = "https://storage.googleapis.com/uploads.linear.app/ws/att/1?X-Goog-Expires=60"
    submit = {"ok": True, "issue_url": "https://linear.app/x/1"}
    with mock.patch("screencap.feedback.requests.post", side_effect=[_Resp(prep_for(ok_url)), _Resp(submit)]), \
        mock.patch("screencap.feedback.requests.put", return_value=_Resp({})) as put:
        env = feedback.send_feedback(
            {"type": "bug", "message": "hi", "attachments": [{"path": str(f1), "content_type": "image/png"}]}
        )
    assert env["ok"] is True
    put.assert_called_once()

    # Foreign bucket as the first path segment, and a dot-segment that a
    # normalizing HTTP client (requests/urllib3 collapse "/../" per RFC 3986)
    # would rewrite to another bucket after a naive prefix check passed. Both
    # must send no bytes.
    for evil_url in (
        "https://storage.googleapis.com/attacker-bucket/uploads.linear.app/1",
        "https://storage.googleapis.com/uploads.linear.app/../attacker-bucket/1",
    ):
        with mock.patch("screencap.feedback.requests.post", return_value=_Resp(prep_for(evil_url))), \
            mock.patch("screencap.feedback.requests.put") as put:
            env = feedback.send_feedback(
                {"type": "bug", "message": "hi", "attachments": [{"path": str(f1), "content_type": "image/png"}]}
            )
        assert env["ok"] is False, evil_url
        put.assert_not_called()         # no bytes left the machine


# --------------------------------------------------------------------------
# Privacy guard (R10) — runs on CI (privacy lane only)
# --------------------------------------------------------------------------
@pytest.mark.privacy
def test_reads_only_supplied_paths_never_recordings(tmp_path):
    f1 = _png(tmp_path)
    prep = {"ok": True, "uploads": [{"uploadUrl": "https://uploads.linear.app/up/1", "assetUrl": "https://uploads.linear.app/a/1", "claim": "c1", "headers": []}]}
    read_paths = []

    def spy_read(path):
        read_paths.append(path)
        return b"bytes"

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(prep if json["action"] == "prepare" else {"ok": True, "issue_url": "u"})

    with mock.patch("screencap.feedback._read_file", side_effect=spy_read), \
        mock.patch("screencap.feedback.requests.post", side_effect=fake_post), \
        mock.patch("screencap.feedback.requests.put", return_value=_Resp(status_code=200)):
        feedback.send_feedback(
            {"type": "bug", "message": "hi", "attachments": [{"path": str(f1), "content_type": "image/png"}]}
        )

    # The only file the client ever read is the one attachment the caller chose.
    assert read_paths == [str(f1)]
    assert all(".screencap/recordings" not in p and "recording.db" not in p for p in read_paths)


# --------------------------------------------------------------------------
# Mirrored constants (KTD-5) — CLI ↔ relay values must be equal
# --------------------------------------------------------------------------
@pytest.mark.privacy
def test_cli_and_relay_constants_match():
    # Extract the relay's constants WITHOUT importing the module: it imports
    # functions_framework/flask (cloud-function-only deps absent from the main
    # test env, e.g. the privacy lane) and evaluates an HTTP decorator at import
    # time. Parse the source and exec only the constant assignments, whose values
    # are pure arithmetic + frozenset builtins — no third-party imports needed.
    relay_path = Path(__file__).resolve().parents[1] / "scripts" / "cloud-function" / "feedback.py"
    wanted = {
        "MAX_FILE_BYTES",
        "MAX_TOTAL_BYTES",
        "MAX_ATTACHMENTS",
        "ALLOWED_CONTENT_TYPES",
        "LINEAR_UPLOAD_HOSTS",
        "LINEAR_UPLOAD_GCS_HOST",
        "LINEAR_UPLOAD_GCS_PREFIX",
    }
    tree = ast.parse(relay_path.read_text())
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted for t in node.targets
        ):
            exec(compile(ast.Module([node], []), str(relay_path), "exec"), ns)
    assert wanted <= ns.keys(), f"relay constants not found: {wanted - ns.keys()}"

    assert feedback.MAX_FILE_BYTES == ns["MAX_FILE_BYTES"]
    assert feedback.MAX_TOTAL_BYTES == ns["MAX_TOTAL_BYTES"]
    assert feedback.MAX_ATTACHMENTS == ns["MAX_ATTACHMENTS"]
    assert feedback.ALLOWED_CONTENT_TYPES == ns["ALLOWED_CONTENT_TYPES"]
    assert feedback.LINEAR_UPLOAD_HOSTS == ns["LINEAR_UPLOAD_HOSTS"]
    assert feedback.LINEAR_UPLOAD_GCS_HOST == ns["LINEAR_UPLOAD_GCS_HOST"]
    assert feedback.LINEAR_UPLOAD_GCS_PREFIX == ns["LINEAR_UPLOAD_GCS_PREFIX"]


# --------------------------------------------------------------------------
# CLI command contract (exit 0 with ok:false on expected failure)
# --------------------------------------------------------------------------
def _run_cli(stdin_text):
    import json as _json

    from click.testing import CliRunner

    from screencap.cli import cli

    # --json mirrors the app's exact argv (CLIClient.runJSONRawStdin asserts
    # the flag is present), so a regression to the option breaks here first.
    return CliRunner().invoke(cli, ["feedback", "send", "--json"], input=stdin_text), _json


def test_cli_send_success_exits_zero():
    payload = '{"type": "bug", "message": "it broke"}'
    with mock.patch(
        "screencap.feedback.requests.post",
        return_value=_Resp({"ok": True, "issue_url": "https://linear.app/x/i", "issue_id": "i"}),
    ):
        result, _json = _run_cli(payload)
    assert result.exit_code == 0
    env = _json.loads(result.output)
    assert env["ok"] is True
    assert env["issue_url"] == "https://linear.app/x/i"


def test_cli_send_expected_failure_still_exits_zero():
    payload = '{"type": "bug", "message": "it broke"}'
    with mock.patch(
        "screencap.feedback.requests.post",
        return_value=_Resp({"ok": False, "error_kind": "rate_limited", "message": "later"}),
    ):
        result, _json = _run_cli(payload)
    assert result.exit_code == 0             # never a non-zero exit for expected failures
    env = _json.loads(result.output)
    assert env["ok"] is False
    assert env["error_kind"] == "rate_limited"


def test_cli_send_bad_json_exits_zero_with_invalid():
    result, _json = _run_cli("not json")
    assert result.exit_code == 0
    env = _json.loads(result.output)
    assert env["error_kind"] == "invalid"
