"""U4 — daemon recording.share verb op-routing (SCR-229).

Exercises the sync ``_run_share_op`` helper with the backend + share_service
mocked, so the create/revoke/list routing + backend cleanup are verified without
a live cloud. Not privacy-marked (daemon-routing, runs in the normal suite,
mirroring test_daemon_recording_rename).
"""

from unittest import mock

import pytest

from screencap import share_service
from screencap.daemon import app, schema


def _req(op, **kw):
    return schema.RecordingShareRequest.model_validate({"op": op, **kw})


@pytest.fixture
def patched_backend(monkeypatch, tmp_path):
    backend = mock.Mock()
    monkeypatch.setattr("screencap.share_backend.DaemonShareBackend", lambda *a, **k: backend)
    monkeypatch.setattr("screencap.config.get_recordings_dir", lambda: tmp_path)
    monkeypatch.setattr("screencap.config.get_base_dir", lambda: tmp_path)
    monkeypatch.setattr("screencap.upload._get_share_base_url", lambda: "https://screencap.sh")
    return backend


def test_list_returns_local_shares(patched_backend):
    patched_backend.list_local_shares.return_value = [{"token": "t", "recording": "r"}]
    out = app._run_share_op(_req("list"))
    assert out == {"shares": [{"token": "t", "recording": "r"}]}
    patched_backend.close.assert_called_once()


def test_create_returns_link_and_closes_backend(patched_backend, monkeypatch):
    monkeypatch.setattr(
        "screencap.share_service.create_share_flow",
        lambda backend, rec, *, site_base_url, expires_days: share_service.ShareResult(
            url=f"{site_base_url}/share/tok#thekey", token="tok", expires_at="2026-08-20"
        ),
    )
    out = app._run_share_op(_req("create", recording_id="rec-1", expires_days=7))
    assert out == {
        "url": "https://screencap.sh/share/tok#thekey",
        "token": "tok",
        "expires_at": "2026-08-20",
    }
    patched_backend.close.assert_called_once()


def test_create_requires_recording_id(patched_backend):
    with pytest.raises(ValueError):
        app._run_share_op(_req("create"))
    patched_backend.close.assert_called_once()  # still cleaned up on error


def test_revoke_calls_flow_and_marks_local(patched_backend, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "screencap.share_service.revoke_share_flow", lambda backend, token: calls.append(token)
    )
    out = app._run_share_op(_req("revoke", token="tok-9"))
    assert out == {"revoked": True, "token": "tok-9"}
    assert calls == ["tok-9"]
    patched_backend.mark_local_revoked.assert_called_once_with("tok-9")


def test_revoke_requires_token(patched_backend):
    with pytest.raises(ValueError):
        app._run_share_op(_req("revoke"))


def test_unknown_op_raises(patched_backend):
    with pytest.raises(ValueError):
        app._run_share_op(_req("bogus"))
