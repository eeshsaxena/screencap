"""Migrate an existing plaintext recall corpus to the encrypted form (search U7 / R5).

At the U8 default-on flip, a machine may already hold a plaintext corpus (CLI
recordings' ``screenshots/*.jpg`` and a plaintext ``content_index.db``). This module
converts it in one idempotent, resumable, interruption-safe pass so **no plaintext
copy remains** (R5), and provides the daemon-start finisher that resumes a flip that
crashed partway.

Guarantees:

- **Per-frame atomicity.** Each still is encrypted to ``<ts>.jpg.enc`` via the
  atomic temp+fsync+replace writer, and only then is the plaintext ``<ts>.jpg``
  unlinked — so a crash leaves the frame either plaintext or encrypted, never a
  half-written cipher a reader could glob. A re-run that finds both forms finishes
  the frame by removing the leftover plaintext.
- **Index rekey via SQLCipher ``sqlcipher_export``** into a sibling file + atomic
  rename, so the index is either fully plaintext or fully encrypted, never torn.
- **Ordering.** Convert stills → rekey index → set the ``corpus_encrypted`` *done*
  marker LAST, so readers never switch to the encrypted paths before the bytes
  exist. The ``corpus_encryption_requested`` *intent* marker is set FIRST so a
  crash mid-flip resumes on the next daemon start.

All still-readers go through :func:`screencap.still_io.open_still`, which handles
both forms during the migration window.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SQLITE_PLAINTEXT_MAGIC = b"SQLite format 3\x00"


@dataclass
class MigrationReport:
    """Outcome of a corpus migration pass."""

    stills_encrypted: int = 0
    index_rekeyed: bool = False
    recordings_scanned: int = 0
    errors: list[str] = field(default_factory=list)


def migrate_recording_stills(recording_dir: Path, key: bytes) -> int:
    """Encrypt every plaintext ``screenshots/*.jpg`` in ``recording_dir`` in place.

    Returns the number of stills newly encrypted. Idempotent + interruption-safe."""
    from screencap import still_io

    screenshots_dir = Path(recording_dir) / "screenshots"
    if not screenshots_dir.is_dir():
        return 0
    count = 0
    for jpg in sorted(screenshots_dir.glob("*.jpg")):
        enc_path = Path(str(jpg) + still_io.ENC_SUFFIX)
        if enc_path.exists():
            # Crash between the durable .enc write and the plaintext unlink — the
            # encrypted form already exists, so just finish by removing the leftover.
            try:
                jpg.unlink()
            except OSError:
                pass
            continue
        try:
            data = jpg.read_bytes()
            still_io.write_encrypted_still(enc_path, data, key)  # atomic + fsynced
            jpg.unlink()  # remove plaintext only AFTER the ciphertext is durable
        except Exception:
            logger.warning("corpus migrate: failed to encrypt still %s", jpg.name, exc_info=True)
            # Roll back a partial .enc so the frame stays cleanly plaintext for retry.
            try:
                if enc_path.exists():
                    enc_path.unlink()
            except OSError:
                pass
            continue
        count += 1
    return count


def _is_plaintext_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(_SQLITE_PLAINTEXT_MAGIC)) == _SQLITE_PLAINTEXT_MAGIC
    except OSError:
        return False


def rekey_content_index(index_path: Path, key: bytes) -> bool:
    """Rekey a plaintext ``content_index.db`` to SQLCipher in place.

    Returns True if a rekey happened, False if the file is absent or already
    encrypted (idempotent). Exports the plaintext DB into an encrypted sibling via
    ``sqlcipher_export`` then atomically renames it over the original."""
    index_path = Path(index_path)
    if not index_path.exists():
        return False
    if not _is_plaintext_sqlite(index_path):
        return False  # already encrypted (or not a sqlite db) → nothing to do

    from pysqlcipher3 import dbapi2 as sc

    sibling = Path(str(index_path) + ".rekey.tmp")
    if sibling.exists():
        sibling.unlink()
    conn = sc.connect(str(index_path))  # opens the plaintext DB (no PRAGMA key)
    try:
        conn.execute(
            f"ATTACH DATABASE ? AS encrypted KEY \"x'{key.hex()}'\"", (str(sibling),)
        )
        conn.execute("SELECT sqlcipher_export('encrypted')")
        conn.execute("DETACH DATABASE encrypted")
    finally:
        conn.close()
    try:
        os.chmod(sibling, 0o600)
    except OSError:
        pass
    os.replace(str(sibling), str(index_path))  # atomic: plaintext → encrypted
    # Drop any stale WAL/SHM sidecars of the old plaintext DB.
    for suffix in ("-wal", "-shm"):
        side = Path(str(index_path) + suffix)
        try:
            if side.exists():
                side.unlink()
        except OSError:
            pass
    return True


def migrate_corpus(
    recordings_dir: Path, index_path: Path, key: bytes
) -> MigrationReport:
    """Convert every recording's stills + rekey the content index. Idempotent."""
    report = MigrationReport()
    recordings_dir = Path(recordings_dir)
    if recordings_dir.is_dir():
        for rec_dir in sorted(p for p in recordings_dir.iterdir() if p.is_dir()):
            # The scrubbed cloud-copy siblings are derived, transient, and hold
            # plaintext masked JPEGs by design — never encrypt them.
            if rec_dir.name.endswith("-scrubbed"):
                continue
            try:
                report.stills_encrypted += migrate_recording_stills(rec_dir, key)
                report.recordings_scanned += 1
            except Exception as exc:  # noqa: BLE001 — one bad dir must not abort
                logger.warning("corpus migrate: %s failed (%s)", rec_dir.name, type(exc).__name__)
                report.errors.append(rec_dir.name)
    try:
        report.index_rekeyed = rekey_content_index(index_path, key)
    except Exception:
        logger.warning("corpus migrate: content-index rekey failed", exc_info=True)
        report.errors.append("content_index.db")
    return report


def flip_corpus_to_encrypted() -> MigrationReport:
    """Perform (or resume) the plaintext→encrypted corpus flip (search U7 / U8).

    Ensures the corpus key, records the intent marker, converts the whole corpus,
    then sets the ``corpus_encrypted`` done marker LAST. Idempotent + resumable."""
    from screencap import config, corpus_crypto
    from screencap.content_index import default_index_path

    key = corpus_crypto.get_or_create_corpus_key()  # raises CorpusKeyUnavailable if impossible
    config.set_corpus_encryption_requested(True)  # persist intent before mutating bytes
    report = migrate_corpus(config.get_recordings_dir(), default_index_path(), key)
    config.set_corpus_encrypted(True)  # done marker — readers now use encrypted paths
    logger.info(
        "corpus migration complete: %d stills encrypted across %d recordings; index rekeyed=%s",
        report.stills_encrypted,
        report.recordings_scanned,
        report.index_rekeyed,
    )
    return report


def resume_at_daemon_start() -> MigrationReport | None:
    """Daemon-start finisher (search U7). Resumes a flip that was requested but not
    completed, and re-asserts convergence when it was. A no-op (returns None) before
    any flip is requested. Strictly fail-open — a migration error never blocks boot."""
    from screencap import config

    if not (config.get_corpus_encryption_requested() or config.get_corpus_encrypted()):
        return None  # pre-flip: nothing to do
    try:
        return flip_corpus_to_encrypted()
    except Exception:  # noqa: BLE001 — migration must never break daemon start
        logger.warning("corpus migration at daemon start failed (will retry next start)", exc_info=True)
        return None


__all__ = [
    "MigrationReport",
    "migrate_recording_stills",
    "rekey_content_index",
    "migrate_corpus",
    "flip_corpus_to_encrypted",
    "resume_at_daemon_start",
]
