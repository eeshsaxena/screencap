"""U2 / U3 — encrypt at both upload seams, decrypt on download.

End-to-end through the real seams with a mocked object store: a captured PUT
body is fed back to the download seam, proving encrypt→upload→download→decrypt
returns byte-identical originals, and that plaintext never ships under the flag.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

import keyring
import pytest
import requests
from cryptography.exceptions import InvalidTag

from screencap import cloud_crypto as cc
from screencap import download, upload
from screencap.chunk_processor import _upload_single
from screencap.upload import FileInfo, _upload_with_progress, request_signed_urls

pytestmark = pytest.mark.privacy

KEY = b"K" * 32


@pytest.fixture
def e2ee_on(monkeypatch):
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "true")
    monkeypatch.delenv(cc.ENGINE_CLOUD_KEY_FILE_ENV, raising=False)
    monkeypatch.setattr(
        keyring, "get_password", lambda s, a: base64.b64encode(KEY).decode()
    )


@pytest.fixture
def e2ee_off(monkeypatch):
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "false")


class _PutResp:
    status_code = 200

    def raise_for_status(self):
        pass


class _GetResp:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"content-length": str(len(body))}
        self.status_code = 200

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def _readall(data) -> bytes:
    return data.read() if hasattr(data, "read") else bytes(data)


def _progress():
    return SimpleNamespace(reset=lambda *a, **k: None, update=lambda *a, **k: None)


def _make_file(tmp_path, name="chunk_0.mp4", content=b"payload-bytes-across-frames" * 8):
    p = tmp_path / name
    p.write_bytes(content)
    return FileInfo(name=name, path=p, content_type="video/mp4", size=len(content)), content


def _capture_put(monkeypatch):
    box: dict = {}

    def fake_put(url, data=None, headers=None, timeout=None):
        box["body"] = _readall(data)
        box["headers"] = headers
        return _PutResp()

    monkeypatch.setattr(requests, "put", fake_put)
    return box


# --------------------------------------------------------------------------
# Batch seam (upload.py _upload_with_progress) round-trip
# --------------------------------------------------------------------------


def test_batch_upload_ciphertext_and_download_round_trip(tmp_path, monkeypatch, e2ee_on):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_with_progress(fi, "https://put", _progress(), 1, "rec", 0)

    body = box["body"]
    assert cc.is_encrypted_prefix(body)  # Covers AE3: server object is ciphertext
    assert box["headers"]["Content-Type"] == "application/octet-stream"
    assert int(box["headers"]["Content-Length"]) == len(body)

    monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None: _GetResp(body))
    dest = tmp_path / "out" / "chunk_0.mp4"
    download._download_file_with_progress("https://get", dest, _progress(), 1)
    assert dest.read_bytes() == content  # Covers AE4: decrypts to the original


def test_batch_upload_plaintext_when_flag_off(tmp_path, monkeypatch, e2ee_off):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_with_progress(fi, "https://put", _progress(), 1, "rec", 0)
    assert box["body"] == content  # byte-identical to today
    assert box["headers"]["Content-Type"] == "video/mp4"


# --------------------------------------------------------------------------
# Live seam (chunk_processor._upload_single) — the plaintext-leak regression
# --------------------------------------------------------------------------


def test_live_upload_is_ciphertext(tmp_path, monkeypatch, e2ee_on):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_single(fi, "https://put")
    assert cc.is_encrypted_prefix(box["body"])
    assert box["headers"]["Content-Type"] == "application/octet-stream"
    # and it round-trips
    monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None: _GetResp(box["body"]))
    dest = tmp_path / "out" / "chunk_0.mp4"
    download._download_file_with_progress("https://get", dest, _progress(), 1)
    assert dest.read_bytes() == content


def test_live_upload_plaintext_when_flag_off(tmp_path, monkeypatch, e2ee_off):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_single(fi, "https://put")
    assert box["body"] == content


# --------------------------------------------------------------------------
# Fail closed: flag on but no key -> never upload plaintext
# --------------------------------------------------------------------------


@pytest.fixture
def e2ee_on_no_key(monkeypatch):
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "true")
    monkeypatch.delenv(cc.ENGINE_CLOUD_KEY_FILE_ENV, raising=False)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: None)


def test_batch_fails_closed_without_key(tmp_path, monkeypatch, e2ee_on_no_key):
    fi, _ = _make_file(tmp_path)
    monkeypatch.setattr(requests, "put", lambda *a, **k: pytest.fail("uploaded without a key"))
    with pytest.raises(upload.CloudEncryptionUnavailable):
        _upload_with_progress(fi, "https://put", _progress(), 1, "rec", 0)


def test_live_fails_closed_without_key(tmp_path, monkeypatch, e2ee_on_no_key):
    fi, _ = _make_file(tmp_path)
    monkeypatch.setattr(requests, "put", lambda *a, **k: pytest.fail("uploaded without a key"))
    with pytest.raises(upload.CloudEncryptionUnavailable):
        _upload_single(fi, "https://put")


# --------------------------------------------------------------------------
# Signed-URL request advertises octet-stream when the flag is on (KTD-5)
# --------------------------------------------------------------------------


def test_signed_url_request_uses_octet_stream_when_flag_on(monkeypatch, e2ee_on):
    box: dict = {}

    def fake_authed_post(post, url, json=None, timeout=None):
        box["payload"] = json
        return SimpleNamespace(status_code=200, json=lambda: {"urls": {}, "gcs_prefix": ""})

    monkeypatch.setattr("screencap.auth.authed_post", fake_authed_post)
    monkeypatch.setattr(upload, "_get_upload_url", lambda: "http://fn")
    fi = FileInfo(name="chunk_0.mp4", path=Path("/x"), content_type="video/mp4", size=1)
    request_signed_urls("rec", [fi])
    assert box["payload"]["files"][0]["content_type"] == "application/octet-stream"


def test_signed_url_request_uses_real_type_when_flag_off(monkeypatch, e2ee_off):
    box: dict = {}

    def fake_authed_post(post, url, json=None, timeout=None):
        box["payload"] = json
        return SimpleNamespace(status_code=200, json=lambda: {"urls": {}, "gcs_prefix": ""})

    monkeypatch.setattr("screencap.auth.authed_post", fake_authed_post)
    monkeypatch.setattr(upload, "_get_upload_url", lambda: "http://fn")
    fi = FileInfo(name="chunk_0.mp4", path=Path("/x"), content_type="video/mp4", size=1)
    request_signed_urls("rec", [fi])
    assert box["payload"]["files"][0]["content_type"] == "video/mp4"


# --------------------------------------------------------------------------
# Download: mixed plaintext passthrough + truncation leaves no partial file
# --------------------------------------------------------------------------


def test_download_plaintext_object_passthrough(tmp_path, monkeypatch):
    body = b'{"schema_version": 1, "events": []}'  # no magic
    monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None: _GetResp(body))
    dest = tmp_path / "events.jsonl"
    download._download_file_with_progress("https://get", dest, _progress(), 1)
    assert dest.read_bytes() == body


def test_download_truncated_ciphertext_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv(cc.ENGINE_CLOUD_KEY_FILE_ENV, raising=False)
    monkeypatch.setattr(
        keyring, "get_password", lambda s, a: base64.b64encode(KEY).decode()
    )
    blob = b"".join(cc.encrypt_stream(io.BytesIO(b"z" * 100), KEY, 16))
    truncated = blob[: cc.HEADER_LEN + (16 + 16)]  # header + first (non-final) frame
    monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None: _GetResp(truncated))
    dest = tmp_path / "enc.mp4"
    with pytest.raises(InvalidTag):
        download._download_file_with_progress("https://get", dest, _progress(), 1)
    assert not dest.exists()


# --------------------------------------------------------------------------
# recording.db exclusion is unchanged by encryption (R10)
# --------------------------------------------------------------------------


def test_assert_uploadable_still_rejects_recording_db(tmp_path):
    from screencap.upload import assert_uploadable

    fi = FileInfo(
        name="recording.db",
        path=tmp_path / "recording.db",
        content_type="application/x-sqlite3",
        size=1,
    )
    with pytest.raises(Exception):
        assert_uploadable(fi)
