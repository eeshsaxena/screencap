"""Share-by-link handler tests (SCR-229, U2/U3) — create / resolve / revoke.

The record store is a persistent in-memory GCS fake keyed by object name, so a
record written by create-share is readable by a later resolve/revoke request.
``verify_bearer`` is patched per-test; ``_credentials`` is already a no-op
MagicMock via conftest.
"""

import contextlib
from unittest import mock

import flask
import main
import pytest
import shares
from auth import AuthInvalid


@pytest.fixture(autouse=True)
def app_context():
    app = flask.Flask(__name__)
    with app.app_context():
        yield


class FakeBlob:
    def __init__(self, name, store):
        self.name = name
        self._store = store

    def exists(self):
        return self.name in self._store

    def download_as_text(self):
        return self._store[self.name].decode("utf-8")

    def upload_from_string(self, data, content_type=None):
        self._store[self.name] = data.encode("utf-8") if isinstance(data, str) else data

    def generate_signed_url(self, **kwargs):
        return f"https://signed.example/{self.name}?m={kwargs.get('method')}"


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return FakeBlob(name, self.store)


@pytest.fixture
def bucket():
    fake = FakeBucket()
    with mock.patch.object(main._bucket, "blob", side_effect=fake.blob):
        yield fake


@contextlib.contextmanager
def _auth(uid=None, exc=None):
    target = mock.patch.object(main, "verify_bearer", side_effect=exc) if exc else \
        mock.patch.object(main, "verify_bearer", return_value=uid)
    with target:
        yield


def _req(body):
    return mock.Mock(
        method="POST",
        headers={"Authorization": "Bearer tok"},
        get_json=mock.Mock(return_value=body),
    )


def _invoke(body, uid="owner-1", auth=True):
    ctx = _auth(uid=uid) if auth else contextlib.nullcontext()
    with ctx:
        resp, status, _ = main.get_upload_urls(_req(body))
    return status, (resp.get_json() if hasattr(resp, "get_json") else resp)


def _create(bucket, uid="owner-1", files=None, expires_days=None):
    body = {"action": "create-share", "files": files or ["video.mp4", "audio.flac"]}
    if expires_days is not None:
        body["expires_days"] = expires_days
    return _invoke(body, uid=uid)


def test_create_share_writes_record_and_returns_put_urls(bucket):
    status, payload = _create(bucket)
    assert status == 200
    assert shares.is_valid_token(payload["token"])
    assert set(payload["urls"]) == {"video.mp4", "audio.flac"}
    assert "m=PUT" in payload["urls"]["video.mp4"]
    rec = shares.loads(bucket.store[shares.record_key(payload["token"])].decode())
    assert rec["owner_uid"] == "owner-1" and rec["revoked"] is False


def test_create_share_requires_auth(bucket):
    with _auth(exc=AuthInvalid("no")):
        resp, status, _ = main.get_upload_urls(
            _req({"action": "create-share", "files": ["v.mp4"]})
        )
    assert status == 401


def test_create_rejects_traversal_artifact(bucket):
    status, _ = _create(bucket, files=["../../etc/passwd"])
    assert status == 400


def test_create_rejects_out_of_range_expiry(bucket):
    status, _ = _create(bucket, expires_days=9999)
    assert status == 400


def test_resolve_share_is_tokenless_and_returns_get_urls(bucket):
    _, created = _create(bucket)
    token = created["token"]
    status, payload = _invoke({"action": "resolve-share", "token": token}, auth=False)
    assert status == 200
    assert set(payload["urls"]) == {"video.mp4", "audio.flac"}
    assert "m=GET" in payload["urls"]["video.mp4"]
    assert payload["view_only"] is True


def test_resolve_unknown_token_404(bucket):
    status, _ = _invoke({"action": "resolve-share", "token": shares.new_token()}, auth=False)
    assert status == 404


def test_resolve_traversal_token_rejected_before_gcs(bucket):
    # A climbing token never reaches a GCS call — rejected on format.
    status, _ = _invoke({"action": "resolve-share", "token": "../users/victim"}, auth=False)
    assert status == 400


def test_revoke_then_resolve_is_gone(bucket):
    _, created = _create(bucket)
    token = created["token"]
    status, _ = _invoke({"action": "revoke-share", "token": token}, uid="owner-1")
    assert status == 200
    # AE1: a revoked share resolves to 410 gone.
    status, payload = _invoke({"action": "resolve-share", "token": token}, auth=False)
    assert status == 410 and payload["code"] == "share_gone"


def test_revoke_by_non_owner_denied_and_share_stays_live(bucket):
    _, created = _create(bucket, uid="owner-1")
    token = created["token"]
    status, _ = _invoke({"action": "revoke-share", "token": token}, uid="attacker")
    assert status == 404  # non-owner can't distinguish "not yours" from "missing"
    status, _ = _invoke({"action": "resolve-share", "token": token}, auth=False)
    assert status == 200  # the real owner's share is untouched


def test_expired_share_resolves_gone(bucket):
    _, created = _create(bucket, expires_days=1)
    token = created["token"]
    rec = shares.loads(bucket.store[shares.record_key(token)].decode())
    rec["expires_at"] = "2000-01-01T00:00:00+00:00"
    bucket.store[shares.record_key(token)] = shares.dumps(rec).encode()
    # AE2: an expired share resolves to 410 gone.
    status, _ = _invoke({"action": "resolve-share", "token": token}, auth=False)
    assert status == 410
