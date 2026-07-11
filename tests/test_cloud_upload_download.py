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


# KTD-4 (SCR-220 U3): the seams take the recording's FROZEN E2EE decision as a
# parameter — the live flag only seeds the intent at start — so the fixtures
# here control key availability, not the flag.
@pytest.fixture
def cloud_key_available(monkeypatch):
    monkeypatch.delenv(cc.ENGINE_CLOUD_KEY_FILE_ENV, raising=False)
    monkeypatch.setattr(
        keyring, "get_password", lambda s, a: base64.b64encode(KEY).decode()
    )


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


def test_batch_upload_ciphertext_and_download_round_trip(
    tmp_path, monkeypatch, cloud_key_available
):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_with_progress(fi, "https://put", _progress(), 1, "rec", 0, e2ee=True)

    body = box["body"]
    assert cc.is_encrypted_prefix(body)  # Covers AE3: server object is ciphertext
    assert box["headers"]["Content-Type"] == "application/octet-stream"
    assert int(box["headers"]["Content-Length"]) == len(body)

    monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None: _GetResp(body))
    dest = tmp_path / "out" / "chunk_0.mp4"
    download._download_file_with_progress("https://get", dest, _progress(), 1)
    assert dest.read_bytes() == content  # Covers AE4: decrypts to the original


def test_batch_upload_plaintext_when_frozen_off(tmp_path, monkeypatch):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_with_progress(fi, "https://put", _progress(), 1, "rec", 0, e2ee=False)
    assert box["body"] == content  # byte-identical to today
    assert box["headers"]["Content-Type"] == "video/mp4"


# --------------------------------------------------------------------------
# Live seam (chunk_processor._upload_single) — the plaintext-leak regression
# --------------------------------------------------------------------------


def test_live_upload_is_ciphertext(tmp_path, monkeypatch, cloud_key_available):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_single(fi, "https://put", e2ee=True)
    assert cc.is_encrypted_prefix(box["body"])
    assert box["headers"]["Content-Type"] == "application/octet-stream"
    # and it round-trips
    monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None: _GetResp(box["body"]))
    dest = tmp_path / "out" / "chunk_0.mp4"
    download._download_file_with_progress("https://get", dest, _progress(), 1)
    assert dest.read_bytes() == content


def test_live_upload_plaintext_when_frozen_off(tmp_path, monkeypatch):
    fi, content = _make_file(tmp_path)
    box = _capture_put(monkeypatch)
    _upload_single(fi, "https://put", e2ee=False)
    assert box["body"] == content


# --------------------------------------------------------------------------
# Fail closed: frozen-on but no key -> never upload plaintext
# --------------------------------------------------------------------------


@pytest.fixture
def no_cloud_key(monkeypatch):
    monkeypatch.delenv(cc.ENGINE_CLOUD_KEY_FILE_ENV, raising=False)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: None)


def test_batch_fails_closed_without_key(tmp_path, monkeypatch, no_cloud_key):
    fi, _ = _make_file(tmp_path)
    monkeypatch.setattr(requests, "put", lambda *a, **k: pytest.fail("uploaded without a key"))
    with pytest.raises(upload.CloudEncryptionUnavailable):
        _upload_with_progress(fi, "https://put", _progress(), 1, "rec", 0, e2ee=True)


def test_live_fails_closed_without_key(tmp_path, monkeypatch, no_cloud_key):
    fi, _ = _make_file(tmp_path)
    monkeypatch.setattr(requests, "put", lambda *a, **k: pytest.fail("uploaded without a key"))
    with pytest.raises(upload.CloudEncryptionUnavailable):
        _upload_single(fi, "https://put", e2ee=True)


# --------------------------------------------------------------------------
# Signed-URL request advertises octet-stream when the frozen bit is on (KTD-5)
# --------------------------------------------------------------------------


def test_signed_url_request_uses_octet_stream_when_e2ee_on(monkeypatch):
    box: dict = {}

    def fake_authed_post(post, url, json=None, timeout=None):
        box["payload"] = json
        return SimpleNamespace(status_code=200, json=lambda: {"urls": {}, "gcs_prefix": ""})

    monkeypatch.setattr("screencap.auth.authed_post", fake_authed_post)
    monkeypatch.setattr(upload, "_get_upload_url", lambda: "http://fn")
    fi = FileInfo(name="chunk_0.mp4", path=Path("/x"), content_type="video/mp4", size=1)
    request_signed_urls("rec", [fi], e2ee=True)
    assert box["payload"]["files"][0]["content_type"] == "application/octet-stream"


def test_signed_url_request_uses_real_type_when_e2ee_off(monkeypatch):
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
# KTD-4 (SCR-220 U3): the frozen per-recording intent bit — not the live flag —
# decides encryption at every upload seam.
# --------------------------------------------------------------------------


def _write_intent(capture_dir: Path, *, cloud_e2ee: bool | None) -> None:
    """Write a minimal frozen ``.recording_intent``; ``None`` omits the field
    (a pre-arc recording whose intent predates SCR-220)."""
    import json

    intent = {
        "version": 2,
        "destination": "cloud",
        "masked_video_upload": False,
        "privacy_mode": "public",
    }
    if cloud_e2ee is not None:
        intent["cloud_e2ee"] = cloud_e2ee
    (capture_dir / ".recording_intent").write_text(json.dumps(intent))


def _run_live_chunk_upload(tmp_path, monkeypatch) -> tuple[dict, bytes]:
    """Drive the live seam (``upload_chunk_files``) end-to-end with the network
    mocked, returning the captured PUT box and the plaintext content."""
    from screencap import chunk_processor

    content = b"live-chunk-payload" * 16
    chunk = tmp_path / "chunk_0000.mp4"
    chunk.write_bytes(content)
    box = _capture_put(monkeypatch)
    monkeypatch.setattr(
        "screencap.upload.request_signed_urls",
        lambda name, infos, **kw: ({"chunk_0000.mp4": "https://put"}, "gs://p/"),
    )
    ok = chunk_processor.upload_chunk_files(
        "rec", [{"name": "chunk_0000.mp4", "path": chunk}], tmp_path,
    )
    box["ok"] = ok
    return box, content


def test_downgrade_pin_frozen_on_flag_off_live_still_ciphertext(
    tmp_path, monkeypatch, cloud_key_available
):
    """The KTD-4 downgrade pin (AE1/AE3): a recording frozen ``cloud_e2ee: true``
    keeps encrypting after the live flag is flipped off mid-life — the flag's
    only role is seeding the intent at recording start."""
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "false")  # mid-life downgrade
    _write_intent(tmp_path, cloud_e2ee=True)
    box, _ = _run_live_chunk_upload(tmp_path, monkeypatch)
    assert box["ok"] is True
    assert cc.is_encrypted_prefix(box["body"])  # NEVER plaintext
    assert box["headers"]["Content-Type"] == "application/octet-stream"


def test_downgrade_pin_frozen_on_no_key_fails_closed_not_plaintext(
    tmp_path, monkeypatch, no_cloud_key
):
    """Frozen-on with no key available fails the chunk closed — the PUT never
    happens, regardless of the live flag being off."""
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "false")
    _write_intent(tmp_path, cloud_e2ee=True)
    box, _ = _run_live_chunk_upload(tmp_path, monkeypatch)
    assert box["ok"] is False  # chunk FAILED, nothing deleted
    assert "body" not in box  # no PUT was attempted


def test_frozen_off_flag_flipped_on_stays_plaintext(
    tmp_path, monkeypatch, cloud_key_available
):
    """A recording frozen ``cloud_e2ee: false`` uploads plaintext even after the
    live flag is flipped on mid-life (per today's path)."""
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "true")  # mid-life upgrade attempt
    _write_intent(tmp_path, cloud_e2ee=False)
    box, content = _run_live_chunk_upload(tmp_path, monkeypatch)
    assert box["ok"] is True
    assert box["body"] == content
    assert box["headers"]["Content-Type"] == "video/mp4"


def test_pre_arc_recording_uploads_plaintext_never_errors(
    tmp_path, monkeypatch, no_cloud_key
):
    """No frozen field (intent predates SCR-220) → treated as frozen-off:
    plaintext per today's path, never an error — even with the flag on and no
    key anywhere."""
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "true")
    _write_intent(tmp_path, cloud_e2ee=None)
    box, content = _run_live_chunk_upload(tmp_path, monkeypatch)
    assert box["ok"] is True
    assert box["body"] == content


def test_batch_seam_reads_frozen_intent_not_live_flag(
    tmp_path, monkeypatch, cloud_key_available
):
    """The batch seam (``upload_recording``) resolves the same frozen bit: a
    frozen-on recording ships ciphertext with the live flag off."""
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "false")
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_intent(rec, cloud_e2ee=True)
    content = b"batch-payload" * 16
    (rec / "chunk_0000.mp4").write_bytes(content)
    box = _capture_put(monkeypatch)
    signed: dict = {}

    def fake_signed(name, infos, **kw):
        signed.update(kw)
        return ({fi.name: "https://put" for fi in infos}, "gs://p/")

    monkeypatch.setattr(upload, "request_signed_urls", fake_signed)
    result = upload.upload_recording(rec, jobs=1)
    assert not result.failed
    assert signed == {"e2ee": True}  # the sign request advertised the frozen bit
    assert cc.is_encrypted_prefix(box["body"])
    assert box["headers"]["Content-Type"] == "application/octet-stream"


def test_intent_frozen_at_start_flag_flip_does_not_change_it(tmp_path, monkeypatch):
    """The bit is seeded from the live flag ONCE at recording start; flipping the
    flag mid-recording does not change the frozen value on disk."""
    import json

    from screencap.engine.config import RecordingConfig
    from screencap.engine.lock_policy import _write_identity_files
    from screencap.engine.screen_recorder import RecordingRequest

    request = RecordingRequest(
        name="frozen-rec", config=RecordingConfig(), cloud_intent=True,
        keep_local=False,
    )
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "true")
    _write_identity_files(tmp_path, request=request, privacy_mode="public")

    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "false")  # mid-recording flip
    intent = json.loads((tmp_path / ".recording_intent").read_text())
    assert intent["cloud_e2ee"] is True

    from screencap.pipeline_chunk_ops import get_frozen_cloud_e2ee

    assert get_frozen_cloud_e2ee(tmp_path) is True


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


# --------------------------------------------------------------------------
# 403-retry re-encrypts from a fresh handle with a fresh nonce (no reuse)
# --------------------------------------------------------------------------


def test_batch_403_retry_reencrypts_with_fresh_nonce(
    tmp_path, monkeypatch, cloud_key_available
):
    fi, content = _make_file(tmp_path)
    bodies: list[bytes] = []
    calls = {"n": 0}

    class _403(_PutResp):
        status_code = 403

    def fake_put(url, data=None, headers=None, timeout=None):
        bodies.append(_readall(data))
        calls["n"] += 1
        return _403() if calls["n"] == 1 else _PutResp()

    monkeypatch.setattr(requests, "put", fake_put)
    # The 403 branch re-requests a signed URL before retrying — advertising
    # octet-stream because a cloud key is set (the single decision).
    resign: dict = {}

    def fake_signed(rec, files, **kw):
        resign.update(kw)
        return ({fi.name: "https://put2"}, "p")

    monkeypatch.setattr(upload, "request_signed_urls", fake_signed)
    _upload_with_progress(
        fi, "https://put1", _progress(), 1, "rec", 1, e2ee=True,
    )  # max_retries=1
    assert resign == {"e2ee": True}

    assert len(bodies) == 2  # first attempt 403'd, second succeeded
    assert all(cc.is_encrypted_prefix(b) for b in bodies)
    assert bodies[0] != bodies[1]  # fresh per-file nonce prefix per attempt (no reuse)
    for i, b in enumerate(bodies):
        monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None, _b=b: _GetResp(_b))
        dest = tmp_path / f"out{i}" / "chunk_0.mp4"
        download._download_file_with_progress("https://get", dest, _progress(), 1)
        assert dest.read_bytes() == content


def test_download_plaintext_shorter_than_magic(tmp_path, monkeypatch):
    body = b"{}"  # 2 bytes, shorter than the 6-byte MAGIC — must pass through
    monkeypatch.setattr(requests, "get", lambda url, stream=None, timeout=None: _GetResp(body))
    dest = tmp_path / "tiny.json"
    download._download_file_with_progress("https://get", dest, _progress(), 1)
    assert dest.read_bytes() == body
