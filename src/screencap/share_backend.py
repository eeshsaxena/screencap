"""Real ShareBackend adapter for the daemon share verb (SCR-229, U4).

Implements the :class:`screencap.share_service.ShareBackend` protocol against the
live cloud client (``upload``/``download``/``auth``), the cloud KEK
(``cloud_crypto``), and a local share tracker. Source resolution enforces the R10
masked-only rule: it uses the reviewed local ``<name>-scrubbed`` sibling when
present, else fetches the uploaded cloud copy — never the raw local screenshots.

Heavy client imports (``requests``/``auth``/``download``) are deferred inside the
I/O methods so importing this module (and ``screencap --help``) stays fast.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from screencap.share_service import (
    CreateShareResponse,
    ShareError,
    ShareSourceMissing,
    SourceArtifact,
)

_SHARES_FILE = "shares.json"


class DaemonShareBackend:
    """Live backend the daemon share verb drives. Call :meth:`close` when done
    (it releases any temp dir used to stage a cloud-fetched source)."""

    def __init__(self, recordings_dir: Path, state_dir: Path) -> None:
        self._recordings_dir = Path(recordings_dir)
        self._state_dir = Path(state_dir)
        self._tempdir: tempfile.TemporaryDirectory | None = None

    # ---- source resolution (R10: masked copy only) --------------------------

    def masked_artifacts(self, recording_name: str) -> list[SourceArtifact]:
        scrubbed = self._recordings_dir / f"{recording_name}-scrubbed"
        if scrubbed.is_dir():
            return self._local_scrubbed_artifacts(scrubbed)
        return self._cloud_fetched_artifacts(recording_name)

    def _local_scrubbed_artifacts(self, scrubbed: Path) -> list[SourceArtifact]:
        """The reviewed local scrubbed copy: plaintext masked artifacts."""
        artifacts = []
        for path in sorted(p for p in scrubbed.rglob("*") if p.is_file()):
            rel = path.relative_to(scrubbed).as_posix()
            artifacts.append(
                SourceArtifact(
                    name=rel, open_reader=(lambda p=path: open(p, "rb")), source_key=None
                )
            )
        return artifacts

    def _cloud_fetched_artifacts(self, recording_name: str) -> list[SourceArtifact]:
        """The uploaded cloud copy — KEK-ciphertext when E2EE was on, else plaintext.

        ``reencrypt_for_share`` detects the shape from the object magic, so
        passing the cloud KEK (or ``None`` when unavailable) is safe: encrypted
        artifacts get decrypted, plaintext ones ignore the key.
        """
        import requests

        from screencap import cloud_crypto, download

        urls, _prefix = download.request_signed_urls(recording_name)
        if not urls:
            # Nothing to share rather than a backend fault: the surfaces render
            # a different instruction for each, so this stays the typed variant.
            raise ShareSourceMissing(
                f"recording {recording_name!r} has no local scrubbed copy and is not in the cloud"
            )
        self._tempdir = tempfile.TemporaryDirectory(prefix="screencap-share-")
        base = Path(self._tempdir.name)
        source_key = cloud_crypto.resolve_cloud_key()  # None ok for plaintext copies

        artifacts = []
        for rel, url in urls.items():
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(url, stream=True, timeout=(10, None)) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            artifacts.append(
                SourceArtifact(
                    name=rel, open_reader=(lambda p=dest: open(p, "rb")), source_key=source_key
                )
            )
        return artifacts

    # ---- cloud function calls ----------------------------------------------

    def create_share(self, artifact_names, expires_days) -> CreateShareResponse:
        import requests

        from screencap import auth, upload

        payload = {"action": "create-share", "files": list(artifact_names)}
        if expires_days is not None:
            payload["expires_days"] = expires_days
        resp = auth.authed_post(
            requests.post, upload._get_upload_url(), json=payload, timeout=30
        )
        if resp.status_code != 200:
            raise ShareError(f"create-share failed ({resp.status_code})")
        data = resp.json()
        return CreateShareResponse(
            token=data["token"], put_urls=data["urls"], expires_at=data["expires_at"]
        )

    def upload(self, put_url, data) -> None:
        import requests

        resp = requests.put(
            put_url,
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=(10, None),
        )
        resp.raise_for_status()

    def revoke_share(self, token) -> None:
        import requests

        from screencap import auth, upload

        resp = auth.authed_post(
            requests.post,
            upload._get_upload_url(),
            json={"action": "revoke-share", "token": token},
            timeout=30,
        )
        if resp.status_code != 200:
            raise ShareError(f"revoke-share failed ({resp.status_code})")

    # ---- local tracking (v1: JSON file; Firestore is the KTD2 scale path) ---

    def record_local_share(self, token, recording_name, expires_at) -> None:
        shares = self.list_local_shares()
        shares.append(
            {"token": token, "recording": recording_name, "expires_at": expires_at}
        )
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / _SHARES_FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(shares))
        os.replace(tmp, path)

    def list_local_shares(self) -> list[dict]:
        try:
            return json.loads((self._state_dir / _SHARES_FILE).read_text())
        except (OSError, ValueError):
            return []

    def mark_local_revoked(self, token) -> None:
        shares = self.list_local_shares()
        for entry in shares:
            if entry.get("token") == token:
                entry["revoked"] = True
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / _SHARES_FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(shares))
        os.replace(tmp, path)

    def close(self) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None
