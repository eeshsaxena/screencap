"""U4 — daemon recording.share verb op-routing (SCR-229).

Exercises the sync ``_run_share_op`` helper with the backend + share_service
mocked, so the create/revoke/list routing + backend cleanup are verified without
a live cloud. The original routing tests are not privacy-marked (daemon-routing,
runs in the normal suite, mirroring test_daemon_recording_rename).

The failure-classification tests added for SCR-299 U1 ARE privacy-marked: CI runs
only the privacy lane, and the property they pin — that a share failure is typed
structurally and that no failing path leaks the token or key into the audit log —
is exactly what must not regress unobserved.
"""

import json
from pathlib import Path
from unittest import mock

import pytest
from httpx import ASGITransport, AsyncClient

from screencap import share_service
from screencap.daemon import app, audit_log, errors, provenance, schema
from screencap.daemon.app import build_app


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


# --- SCR-299 U1: typed failure classification -------------------------------
#
# The app renders recovery copy keyed on these codes and never on message text
# (KTD2), so each case below pins the STRUCTURAL mapping. A reworded backend
# message must not be able to reclassify a failure.


def _raising(exc):
    def _fail(*_a, **_kw):
        raise exc

    return _fail


@pytest.mark.privacy
def test_signed_out_create_maps_to_not_signed_in(patched_backend, monkeypatch):
    from screencap import auth

    monkeypatch.setattr(
        "screencap.share_service.create_share_flow", _raising(auth.NotSignedIn("no credential"))
    )
    with pytest.raises(errors.DaemonAPIError) as caught:
        app._run_share_op(_req("create", recording_id="rec-1"))
    assert caught.value.error_code == errors.NOT_SIGNED_IN
    patched_backend.close.assert_called_once()


@pytest.mark.privacy
def test_signed_out_revoke_maps_to_not_signed_in(patched_backend, monkeypatch):
    """Revoke shares create's vocabulary — one mapper, not a per-op dialect."""
    from screencap import auth

    monkeypatch.setattr(
        "screencap.share_service.revoke_share_flow", _raising(auth.NotSignedIn("no credential"))
    )
    with pytest.raises(errors.DaemonAPIError) as caught:
        app._run_share_op(_req("revoke", token="tok-9"))
    assert caught.value.error_code == errors.NOT_SIGNED_IN


@pytest.mark.privacy
def test_missing_cloud_copy_maps_to_share_unavailable(patched_backend, monkeypatch):
    """A recording with nothing uploaded is a distinct, non-retryable refusal.

    Defense in depth, not what satisfies AE3 — the app keeps the action
    unavailable so a user never reaches this path.
    """
    monkeypatch.setattr(
        "screencap.share_service.create_share_flow",
        _raising(share_service.ShareSourceMissing("no masked artifacts")),
    )
    with pytest.raises(errors.DaemonAPIError) as caught:
        app._run_share_op(_req("create", recording_id="rec-1"))
    assert caught.value.error_code == errors.SHARE_UNAVAILABLE


@pytest.mark.privacy
def test_backend_failure_maps_to_backend_unavailable(patched_backend, monkeypatch):
    """A cloud-function failure is retryable — distinct from nothing-to-share."""
    monkeypatch.setattr(
        "screencap.share_service.create_share_flow",
        _raising(share_service.ShareError("create-share failed (502)")),
    )
    with pytest.raises(errors.DaemonAPIError) as caught:
        app._run_share_op(_req("create", recording_id="rec-1"))
    assert caught.value.error_code == errors.SHARE_BACKEND_UNAVAILABLE


@pytest.mark.privacy
def test_transport_failure_maps_to_backend_unavailable(patched_backend, monkeypatch):
    import requests

    monkeypatch.setattr(
        "screencap.share_service.create_share_flow",
        _raising(requests.exceptions.ConnectionError("dns")),
    )
    with pytest.raises(errors.DaemonAPIError) as caught:
        app._run_share_op(_req("create", recording_id="rec-1"))
    assert caught.value.error_code == errors.SHARE_BACKEND_UNAVAILABLE


@pytest.mark.privacy
def test_source_missing_beats_generic_share_error(patched_backend, monkeypatch):
    """Ordering guard: ShareSourceMissing subclasses ShareError, so a mapper that
    tested the base class first would collapse both into the retryable code and
    tell a user with nothing uploaded to 'try again'."""
    monkeypatch.setattr(
        "screencap.share_service.create_share_flow",
        _raising(share_service.ShareSourceMissing("no masked artifacts")),
    )
    with pytest.raises(errors.DaemonAPIError) as caught:
        app._run_share_op(_req("create", recording_id="rec-1"))
    assert caught.value.error_code != errors.SHARE_BACKEND_UNAVAILABLE


@pytest.mark.privacy
def test_unrecognized_exception_stays_untyped(patched_backend, monkeypatch):
    """The catch-all is NARROWED, not removed — an unexpected bug must still
    reach the generic internal-error path rather than being mislabeled."""
    monkeypatch.setattr(
        "screencap.share_service.create_share_flow", _raising(RuntimeError("unexpected"))
    )
    with pytest.raises(RuntimeError):
        app._run_share_op(_req("create", recording_id="rec-1"))


# --- SCR-299 U1: route envelope + audit-log exclusion ------------------------


@pytest.fixture
def audit_log_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    return target


def _patch_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=4321,
            path="/usr/local/bin/screencap",
            classification=provenance.STARTED_BY_CLI,
        ),
    )


async def _post_share(payload: dict):
    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v0/recording.share", json=payload)


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_signed_out_create_returns_typed_envelope(
    patched_backend, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap import auth

    _patch_peer(monkeypatch)
    monkeypatch.setattr(
        "screencap.share_service.create_share_flow", _raising(auth.NotSignedIn("no credential"))
    )

    resp = await _post_share({"op": "create", "recording_id": "rec-1"})

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == errors.NOT_SIGNED_IN

    record = json.loads(audit_log_at.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verb"] == "recording.share"
    assert record["outcome"] == errors.NOT_SIGNED_IN
    assert record["peer_pid"] == 4321


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_failing_share_never_logs_token_or_key(
    patched_backend, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole encryption model rests on the key never reaching the server or
    its logs. A failure carrying both in its message must still audit to peer +
    outcome only."""
    _patch_peer(monkeypatch)
    token = "TOKENabc123"
    key = "KEYxyz789"
    monkeypatch.setattr(
        "screencap.share_service.create_share_flow",
        _raising(share_service.ShareError(f"failed for {token}#{key}")),
    )

    resp = await _post_share({"op": "create", "recording_id": "rec-1"})
    assert resp.status_code == 503, resp.text

    raw = audit_log_at.read_text(encoding="utf-8")
    assert token not in raw
    assert key not in raw
    assert token not in resp.text
    assert key not in resp.text
