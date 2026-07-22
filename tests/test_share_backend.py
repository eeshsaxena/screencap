"""U4 — real DaemonShareBackend adapter (SCR-229). Privacy-bearing, Vision-free.

Cloud I/O is mocked; the source-resolution, local tracking, and an end-to-end
run through the real backend + share_service are exercised for real.
"""

import io
from unittest import mock

import pytest

from screencap import cloud_crypto, share_backend, share_crypto, share_service

pytestmark = pytest.mark.privacy


def _decrypt(ciphertext, key):
    out = io.BytesIO()
    cloud_crypto.decrypt_to(io.BytesIO(ciphertext), out, key)
    return out.getvalue()


def test_masked_artifacts_uses_scrubbed_copy_not_raw(tmp_path):
    recordings = tmp_path / "recordings"
    scrubbed = recordings / "rec-scrubbed" / "screenshots"
    scrubbed.mkdir(parents=True)
    (recordings / "rec-scrubbed" / "video.mp4").write_bytes(b"masked video")
    (scrubbed / "0.jpg").write_bytes(b"masked shot")
    # A raw recording dir that must never be sourced.
    (recordings / "rec").mkdir()
    (recordings / "rec" / "video.mp4").write_bytes(b"RAW unmasked")

    be = share_backend.DaemonShareBackend(recordings, tmp_path / "state")
    arts = be.masked_artifacts("rec")
    names = sorted(a.name for a in arts)
    assert names == ["screenshots/0.jpg", "video.mp4"]
    assert all(a.source_key is None for a in arts)  # scrubbed copy is plaintext
    # Reader yields the masked bytes, not the raw recording dir's bytes.
    by = {a.name: a for a in arts}
    with by["video.mp4"].open_reader() as fh:
        assert fh.read() == b"masked video"


def test_local_share_tracking_round_trips_across_instances(tmp_path):
    state = tmp_path / "state"
    be = share_backend.DaemonShareBackend(tmp_path / "rec", state)
    be.record_local_share("tok-1", "rec-a", "2026-08-20")
    be.record_local_share("tok-2", "rec-b", "2026-08-21")
    # A fresh instance reads the persisted file.
    listed = share_backend.DaemonShareBackend(tmp_path / "rec", state).list_local_shares()
    assert [s["token"] for s in listed] == ["tok-1", "tok-2"]
    be.mark_local_revoked("tok-1")
    revoked = {s["token"]: s.get("revoked") for s in be.list_local_shares()}
    assert revoked == {"tok-1": True, "tok-2": None}


def test_create_share_posts_correct_action_and_parses(tmp_path):
    be = share_backend.DaemonShareBackend(tmp_path, tmp_path)
    captured = {}

    def fake_authed_post(post_fn, url, json=None, timeout=None):
        captured["json"] = json
        return mock.Mock(
            status_code=200,
            json=mock.Mock(
                return_value={"token": "t" * 40, "urls": {"a": "put://a"}, "expires_at": "2026-08-20"}
            ),
        )

    with mock.patch("screencap.auth.authed_post", fake_authed_post), \
         mock.patch("screencap.upload._get_upload_url", lambda: "https://fn"):
        resp = be.create_share(["a"], 7)
    assert captured["json"] == {"action": "create-share", "files": ["a"], "expires_days": 7}
    assert resp.token == "t" * 40 and resp.put_urls == {"a": "put://a"}


def test_upload_puts_octet_stream(tmp_path):
    be = share_backend.DaemonShareBackend(tmp_path, tmp_path)
    seen = {}

    def fake_put(url, data=None, headers=None, timeout=None):
        seen.update(url=url, data=data, headers=headers)
        return mock.Mock(raise_for_status=mock.Mock())

    with mock.patch("requests.put", fake_put):
        be.upload("put://x", b"ciphertext")
    assert seen["data"] == b"ciphertext"
    assert seen["headers"]["Content-Type"] == "application/octet-stream"


def test_revoke_share_posts_action(tmp_path):
    be = share_backend.DaemonShareBackend(tmp_path, tmp_path)
    captured = {}

    def fake_authed_post(post_fn, url, json=None, timeout=None):
        captured["json"] = json
        return mock.Mock(status_code=200)

    with mock.patch("screencap.auth.authed_post", fake_authed_post), \
         mock.patch("screencap.upload._get_upload_url", lambda: "https://fn"):
        be.revoke_share("tok-9")
    assert captured["json"] == {"action": "revoke-share", "token": "tok-9"}


def test_end_to_end_real_backend_sources_masked_only(tmp_path):
    recordings = tmp_path / "recordings"
    scrubbed = recordings / "rec-scrubbed"
    scrubbed.mkdir(parents=True)
    (scrubbed / "video.mp4").write_bytes(b"\x00masked video " * 3000)
    (scrubbed / "events.jsonl").write_bytes(b"masked events " * 1000)
    # Raw recording dir with unmasked content that must never be shared.
    (recordings / "rec" / "screenshots").mkdir(parents=True)
    (recordings / "rec" / "screenshots" / "0.jpg").write_bytes(b"UNMASKED raw screenshot")

    uploaded = {}

    def fake_authed_post(post_fn, url, json=None, timeout=None):
        return mock.Mock(
            status_code=200,
            json=mock.Mock(
                return_value={
                    "token": "tok_" + "x" * 40,
                    "urls": {n: f"put://{n}" for n in json["files"]},
                    "expires_at": "2026-08-20T00:00:00+00:00",
                }
            ),
        )

    def fake_put(url, data=None, headers=None, timeout=None):
        uploaded[url] = data
        return mock.Mock(raise_for_status=mock.Mock())

    with mock.patch("screencap.auth.authed_post", fake_authed_post), \
         mock.patch("screencap.upload._get_upload_url", lambda: "https://fn"), \
         mock.patch("requests.put", fake_put):
        be = share_backend.DaemonShareBackend(recordings, tmp_path / "state")
        result = share_service.create_share_flow(be, "rec", site_base_url="https://screencap.sh")

    key = share_crypto.fragment_to_share_key(result.url.split("#", 1)[1])
    for name in ("video.mp4", "events.jsonl"):
        assert _decrypt(uploaded[f"put://{name}"], key) == (scrubbed / name).read_bytes()
    # The raw unmasked screenshot never left the machine.
    assert b"UNMASKED raw screenshot" not in b"".join(uploaded.values())
    assert be.list_local_shares()[0]["recording"] == "rec"
