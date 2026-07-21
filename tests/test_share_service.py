"""U4 — share orchestration (SCR-229). Verifies the source-safety and
key-confidentiality invariants end-to-end. Privacy-bearing, Vision-free."""

import io

import pytest

from screencap import cloud_crypto, share_crypto, share_service

pytestmark = pytest.mark.privacy

_SITE = "https://screencap.sh"


class FakeBackend:
    """Records everything the backend "sees" so tests can assert the share key
    never leaves the orchestration layer."""

    def __init__(self, artifacts):
        self._artifacts = artifacts
        self.create_calls = []
        self.uploaded = {}          # put_url -> data
        self.local_shares = []
        self.revoked = []
        self._token = "T0ken_" + "x" * 40  # valid-shaped

    def masked_artifacts(self, recording_name):
        return self._artifacts

    def create_share(self, artifact_names, expires_days):
        self.create_calls.append((list(artifact_names), expires_days))
        return share_service.CreateShareResponse(
            token=self._token,
            put_urls={name: f"put://{name}" for name in artifact_names},
            expires_at="2026-08-20T00:00:00+00:00",
        )

    def upload(self, put_url, data):
        self.uploaded[put_url] = data

    def record_local_share(self, token, recording_name, expires_at):
        self.local_shares.append((token, recording_name, expires_at))

    def revoke_share(self, token):
        self.revoked.append(token)


def _plaintext_artifact(name, data):
    return share_service.SourceArtifact(
        name=name, open_reader=lambda: io.BytesIO(data), source_key=None
    )


def _kek_ciphertext_artifact(name, data, kek):
    ct = b"".join(cloud_crypto.encrypt_stream(io.BytesIO(data), kek))
    return share_service.SourceArtifact(
        name=name, open_reader=lambda: io.BytesIO(ct), source_key=kek
    )


def _decrypt(ciphertext, key):
    out = io.BytesIO()
    cloud_crypto.decrypt_to(io.BytesIO(ciphertext), out, key)
    return out.getvalue()


def _key_from_url(url):
    return share_crypto.fragment_to_share_key(url.split("#", 1)[1])


def test_create_share_flow_end_to_end_recipient_can_decrypt():
    originals = {"events.jsonl": b"masked events " * 2000, "video.mp4": b"\x00masked video " * 5000}
    backend = FakeBackend(
        [_plaintext_artifact(n, d) for n, d in originals.items()]
    )
    result = share_service.create_share_flow(backend, "rec-1", site_base_url=_SITE)

    assert result.url.startswith(f"{_SITE}/share/{backend._token}#")
    key = _key_from_url(result.url)
    # Every uploaded artifact decrypts with the ONE key carried in the fragment.
    for name, original in originals.items():
        assert _decrypt(backend.uploaded[f"put://{name}"], key) == original
    assert backend.local_shares == [(backend._token, "rec-1", result.expires_at)]


def test_single_key_reused_across_all_artifacts():
    backend = FakeBackend(
        [_plaintext_artifact("a", b"aaa" * 3000), _plaintext_artifact("b", b"bbb" * 3000)]
    )
    result = share_service.create_share_flow(backend, "rec", site_base_url=_SITE)
    key = _key_from_url(result.url)
    # Both decrypt with the same fragment key (one per-recording key, not per-artifact).
    assert _decrypt(backend.uploaded["put://a"], key) == b"aaa" * 3000
    assert _decrypt(backend.uploaded["put://b"], key) == b"bbb" * 3000


def test_mixed_encrypted_and_plaintext_sources():
    kek = b"K" * 32  # E2EE-on source
    backend = FakeBackend(
        [
            _kek_ciphertext_artifact("enc.mp4", b"secret masked " * 4000, kek),
            _plaintext_artifact("plain.jsonl", b"server-readable " * 2000),
        ]
    )
    result = share_service.create_share_flow(backend, "rec", site_base_url=_SITE)
    key = _key_from_url(result.url)
    assert _decrypt(backend.uploaded["put://enc.mp4"], key) == b"secret masked " * 4000
    assert _decrypt(backend.uploaded["put://plain.jsonl"], key) == b"server-readable " * 2000


def test_share_key_never_reaches_the_backend():
    backend = FakeBackend([_plaintext_artifact("v", b"data" * 5000)])
    result = share_service.create_share_flow(backend, "rec", site_base_url=_SITE, expires_days=7)
    key = _key_from_url(result.url)

    # The raw key never appears in any uploaded ciphertext...
    assert all(key not in blob for blob in backend.uploaded.values())
    # ...create-share was told only names + expiry (no key)...
    assert backend.create_calls == [(["v"], 7)]
    # ...and the local record carries no key.
    assert all(key not in str(entry).encode() for entry in backend.local_shares)


def test_no_masked_artifacts_raises():
    with pytest.raises(share_service.ShareError):
        share_service.create_share_flow(FakeBackend([]), "empty", site_base_url=_SITE)


def test_revoke_flow_delegates_to_backend():
    backend = FakeBackend([])
    share_service.revoke_share_flow(backend, "some-token")
    assert backend.revoked == ["some-token"]
