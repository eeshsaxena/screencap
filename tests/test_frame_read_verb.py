"""frame.read decrypt-and-serve verb (search guardrails U8 / KTD6 / KTD2).

Exercises the resolution core ``_run_frame_read``: ALLOW + scrubbed-chunk + size cap
+ decrypt. The blocked-frame integration is covered by frame.nearest, so here the
blocked predicate is stubbed (allow-all / block-all) to isolate the read logic.
Vision-free; the corpus key comes from the env-file channel.
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from screencap import corpus_crypto, frame_blocked, scrub_state, still_io
from screencap.daemon.app import _run_frame_read

pytestmark = pytest.mark.privacy

_TS = 150.0
_STEM = f"{_TS:.6f}"
_TS_MS = round(_TS * 1000)


def _jpeg(color="white", size=(80, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
def rec(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    (recordings / "rec-1" / "screenshots").mkdir(parents=True)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings))
    # Isolate from blocked-frame machinery: allow all frames by default.
    monkeypatch.setattr(frame_blocked, "build_is_blocked", lambda *a, **k: (lambda _ts: False))
    return recordings / "rec-1"


@pytest.fixture
def corpus_key(tmp_path, monkeypatch):
    key = os.urandom(32)
    key_file = tmp_path / "corpus.key"
    key_file.write_text(corpus_crypto._encode_key(key))
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(key_file))
    return key


def test_encrypted_scrubbed_allow_frame_served(rec, corpus_key):
    data = _jpeg("white")
    still_io.write_encrypted_still(rec / "screenshots" / f"{_STEM}.jpg.enc", data, corpus_key)
    scrub_state.mark_chunk_scrubbed(rec, _TS_MS - 1000, _TS_MS + 1000)

    result = _run_frame_read("rec-1", _STEM, max_bytes=8_000_000)
    assert result is not None
    served, content_type = result
    assert content_type == "image/jpeg"
    assert served == data  # decrypted back to the original


def test_encrypted_unscrubbed_chunk_refused(rec, corpus_key):
    still_io.write_encrypted_still(rec / "screenshots" / f"{_STEM}.jpg.enc", _jpeg(), corpus_key)
    # No mark_chunk_scrubbed → the chunk is unscrubbed → refuse (KTD2 fail-closed).
    assert _run_frame_read("rec-1", _STEM, max_bytes=8_000_000) is None


def test_blocked_frame_refused(rec, corpus_key, monkeypatch):
    still_io.write_encrypted_still(rec / "screenshots" / f"{_STEM}.jpg.enc", _jpeg(), corpus_key)
    scrub_state.mark_chunk_scrubbed(rec, _TS_MS - 1000, _TS_MS + 1000)
    # Now block all frames → refuse even though scrubbed.
    monkeypatch.setattr(frame_blocked, "build_is_blocked", lambda *a, **k: (lambda _ts: True))
    assert _run_frame_read("rec-1", _STEM, max_bytes=8_000_000) is None


def test_missing_still_is_a_miss(rec, corpus_key):
    assert _run_frame_read("rec-1", _STEM, max_bytes=8_000_000) is None


def test_size_cap_refuses_oversize(rec, corpus_key):
    still_io.write_encrypted_still(rec / "screenshots" / f"{_STEM}.jpg.enc", _jpeg(), corpus_key)
    scrub_state.mark_chunk_scrubbed(rec, _TS_MS - 1000, _TS_MS + 1000)
    assert _run_frame_read("rec-1", _STEM, max_bytes=8) is None  # tiny cap


def test_plaintext_allow_frame_served(rec):
    data = _jpeg("white")
    (rec / "screenshots" / f"{_STEM}.jpg").write_bytes(data)
    result = _run_frame_read("rec-1", _STEM, max_bytes=8_000_000)
    assert result is not None
    assert result[0] == data  # plaintext served as-is (no scrub-state gate pre-flip)


def test_missing_key_on_encrypted_refused(rec, tmp_path, monkeypatch):
    key = os.urandom(32)
    still_io.write_encrypted_still(rec / "screenshots" / f"{_STEM}.jpg.enc", _jpeg(), key)
    scrub_state.mark_chunk_scrubbed(rec, _TS_MS - 1000, _TS_MS + 1000)
    # Point the key channel at an absent file → load returns None → refuse.
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(tmp_path / "absent.key"))
    assert _run_frame_read("rec-1", _STEM, max_bytes=8_000_000) is None


def test_plaintext_refused_post_flip_when_unscrubbed(rec, monkeypatch):
    # #4b: once the corpus is encrypted, a leftover PLAINTEXT still in an unscrubbed
    # chunk must NOT be served (its secret may be unredacted).
    import screencap.config as cfg

    monkeypatch.setattr(cfg, "get_corpus_encrypted", lambda: True)
    (rec / "screenshots" / f"{_STEM}.jpg").write_bytes(_jpeg())
    assert _run_frame_read("rec-1", _STEM, max_bytes=8_000_000) is None


def test_plaintext_served_post_flip_when_scrubbed(rec, monkeypatch):
    import screencap.config as cfg

    monkeypatch.setattr(cfg, "get_corpus_encrypted", lambda: True)
    data = _jpeg("white")
    (rec / "screenshots" / f"{_STEM}.jpg").write_bytes(data)
    scrub_state.mark_chunk_scrubbed(rec, _TS_MS - 1000, _TS_MS + 1000)
    result = _run_frame_read("rec-1", _STEM, max_bytes=8_000_000)
    assert result is not None and result[0] == data


@pytest.mark.asyncio
async def test_frame_read_route_envelope_and_validation(rec, corpus_key, monkeypatch):
    # #10: route-level coverage — envelope shape, miss -> null, schema validation.
    import httpx

    import screencap.config as cfg
    from screencap.daemon.app import build_app

    monkeypatch.setattr(cfg, "get_local_paywall_enforced", lambda: False)
    data = _jpeg("white")
    still_io.write_encrypted_still(rec / "screenshots" / f"{_STEM}.jpg.enc", data, corpus_key)
    scrub_state.mark_chunk_scrubbed(rec, _TS_MS - 1000, _TS_MS + 1000)

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        ok = await client.post("/v0/frame.read", json={"recording": "rec-1", "stem": _STEM})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["ok"] is True
        assert body["image_base64"] is not None
        assert body["content_type"] == "image/jpeg"

        miss = await client.post(
            "/v0/frame.read", json={"recording": "rec-1", "stem": "999.000000"}
        )
        assert miss.status_code == 200
        assert miss.json()["image_base64"] is None  # a miss is a null envelope, not a 500

        # #16: a traversal-shaped / non-numeric stem is rejected at the schema boundary.
        bad = await client.post(
            "/v0/frame.read", json={"recording": "rec-1", "stem": "../secrets"}
        )
        assert bad.status_code >= 400
        assert bad.json().get("ok") is False
