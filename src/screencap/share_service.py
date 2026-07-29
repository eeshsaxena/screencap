"""Share-by-link orchestration (SCR-229, U4).

Composes the flow the daemon ``/v0/recording.share`` verb drives: resolve the
MASKED source for a recording, re-encrypt it under one per-recording share key,
register the share with the cloud, upload the ciphertext, and return the link.

Two invariants live here, where they can be unit-tested end-to-end:

* **Source safety (R10).** A share is always built from the masked
  ``<name>-scrubbed`` cloud copy (the reviewed local sibling when present, else
  the artifacts fetched from the cloud recording) — never the raw local
  screenshots. The backend supplies the masked artifacts; this layer refuses to
  proceed with none.
* **Key confidentiality (KD4).** The per-recording share key is minted here and
  encoded into the URL ``#fragment`` only. It is NEVER passed to
  ``backend.create_share`` or ``backend.upload`` — the server and its logs only
  ever see the token and ciphertext.

The blocking I/O (fetch, cloud-function calls, PUT upload, local tracking) is
behind :class:`ShareBackend` so the orchestration is testable with fakes and the
daemon verb supplies the real adapter.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable, Protocol

from screencap import share_crypto


class ShareError(RuntimeError):
    """A share could not be produced (no masked source, backend failure)."""


class ShareSourceMissing(ShareError):
    """There is nothing to share: the recording has no masked copy, locally or
    in the cloud.

    A distinct TYPE rather than a ShareError message variant so the daemon can
    map it to its own error code structurally (SCR-299 KTD2). The surfaces key
    their recovery copy on that code, and "you have nothing uploaded" is a
    different instruction from "the backend is down, try again" — matching on
    message text would let a reworded backend string silently swap the two.

    Subclasses :class:`ShareError`, so existing callers that catch the base
    class are unaffected; mappers must test this class FIRST.
    """


@dataclass(frozen=True)
class SourceArtifact:
    """One masked artifact to include in a share.

    ``source_key`` is the cloud KEK when the masked copy is KEK-ciphertext (E2EE
    was on) and ``None`` when it is server-readable plaintext (E2EE off);
    :func:`share_crypto.reencrypt_for_share` branches on it.
    """

    name: str
    open_reader: Callable[[], io.BufferedIOBase]
    source_key: bytes | None


@dataclass(frozen=True)
class CreateShareResponse:
    token: str
    put_urls: dict[str, str]
    expires_at: str


class ShareBackend(Protocol):
    def masked_artifacts(self, recording_name: str) -> list[SourceArtifact]: ...

    def create_share(
        self, artifact_names: list[str], expires_days: int | None
    ) -> CreateShareResponse: ...

    def upload(self, put_url: str, data: bytes) -> None: ...

    def record_local_share(
        self, token: str, recording_name: str, expires_at: str
    ) -> None: ...

    def revoke_share(self, token: str) -> None: ...


@dataclass(frozen=True)
class ShareResult:
    url: str
    token: str
    expires_at: str


def create_share_flow(
    backend: ShareBackend,
    recording_name: str,
    *,
    site_base_url: str,
    expires_days: int | None = None,
) -> ShareResult:
    """Produce a share link for ``recording_name``.

    Raises:
        ShareSourceMissing: the recording has no masked artifacts to share.
    """
    artifacts = backend.masked_artifacts(recording_name)
    if not artifacts:
        raise ShareSourceMissing(f"no shareable (masked) artifacts for {recording_name!r}")

    names = [a.name for a in artifacts]
    by_name = {a.name: a for a in artifacts}
    share_key = share_crypto.mint_share_key()  # never sent to the backend

    resp = backend.create_share(names, expires_days)
    for name, put_url in resp.put_urls.items():
        art = by_name[name]
        buf = io.BytesIO()
        with art.open_reader() as src:
            share_crypto.reencrypt_for_share(src, art.source_key, share_key, buf)
        backend.upload(put_url, buf.getvalue())

    backend.record_local_share(resp.token, recording_name, resp.expires_at)

    fragment = share_crypto.share_key_to_fragment(share_key)
    url = f"{site_base_url.rstrip('/')}/share/{resp.token}#{fragment}"
    return ShareResult(url=url, token=resp.token, expires_at=resp.expires_at)


def revoke_share_flow(backend: ShareBackend, token: str) -> None:
    """Revoke a share by token via the backend."""
    backend.revoke_share(token)
