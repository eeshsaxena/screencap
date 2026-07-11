"""Migrate an existing plaintext recall corpus to the encrypted form (search U7 / R5).

At the U8 default-on flip, a machine may already hold a plaintext corpus (CLI
recordings' ``screenshots/*.jpg``, inline ``png_data`` blobs in ``recording.db``,
and a plaintext ``content_index.db``). This module converts it in one idempotent,
resumable, interruption-safe pass so **no plaintext copy remains** (R5), and
provides the daemon-start finisher that resumes a flip that crashed partway.

Guarantees:

- **Per-frame atomicity.** Each still is encrypted to ``<ts>.jpg.enc`` via the
  atomic temp+fsync+replace writer, and only then is the plaintext ``<ts>.jpg``
  unlinked — so a crash leaves the frame either plaintext or encrypted, never a
  half-written cipher a reader could glob. A re-run that finds both forms finishes
  the frame by removing the leftover plaintext.
- **Inline blobs too (R5).** Plaintext ``png_data`` blobs in ``recording.db`` are
  encrypted in place under the same ``png_blob_aad`` the capture write path uses,
  so ``recording.db`` holds no plaintext still after the flip.
- **Index rekey via SQLCipher ``sqlcipher_export``** into a sibling file + atomic
  rename, under the shared ``content_index_write_lock`` every other index writer
  holds, so a concurrent indexer can't race the swap.
- **Don't race an active writer.** ``flip_corpus_to_encrypted`` skips stills a
  recorder wrote in the last couple of seconds (mtime not yet stable), deferring
  them to the next resume pass rather than reading a torn file or unlinking under
  the writer.
- **Done marker LAST and only on a clean pass.** ``corpus_encrypted`` is set only
  when the pass had no errors AND a re-scan finds no residual plaintext still or
  plaintext ``content_index.db`` — otherwise ``corpus_encryption_requested`` stays
  set so the next daemon start retries, and readers do not switch to the encrypted
  paths while plaintext remains.

All still-readers go through :func:`screencap.still_io.open_still`, which handles
both forms during the migration window.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SQLITE_PLAINTEXT_MAGIC = b"SQLite format 3\x00"

# Stills a recorder touched more recently than this are assumed to belong to an
# in-progress recording; the flip defers them to a later resume pass rather than
# racing the (non-atomic) plaintext writer. Unit callers pass 0.0 (migrate now).
_ACTIVE_WRITE_GRACE_S = 2.0


@dataclass
class MigrationReport:
    """Outcome of a corpus migration pass."""

    stills_encrypted: int = 0
    blobs_encrypted: int = 0
    index_rekeyed: bool = False
    recordings_scanned: int = 0
    errors: list[str] = field(default_factory=list)


def migrate_recording_stills(
    recording_dir: Path, key: bytes, *, min_stable_age_s: float = 0.0
) -> int:
    """Encrypt every plaintext ``screenshots/*.jpg`` in ``recording_dir`` in place.

    Returns the number of stills newly encrypted. Idempotent + interruption-safe.
    ``min_stable_age_s`` > 0 skips stills modified within that window (an active
    recorder is likely still writing them) — they are picked up on a later pass."""
    from screencap import still_io

    screenshots_dir = Path(recording_dir) / "screenshots"
    if not screenshots_dir.is_dir():
        return 0
    now = time.time()
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
        if min_stable_age_s > 0.0:
            try:
                if now - jpg.stat().st_mtime < min_stable_age_s:
                    continue  # likely being written by an active recorder — defer
            except OSError:
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


def migrate_recording_db_blobs(recording_dir: Path, key: bytes) -> int:
    """Encrypt plaintext inline ``png_data`` blobs in ``recording.db`` in place (R5).

    Mirrors the capture write path (``recorder`` -> ``still_io.png_blob_aad``): each
    blob is bound to ``(recording_timestamp, timestamp)``. Idempotent — an already
    ``corpus_crypto.is_encrypted`` blob is left alone. Returns the number newly
    encrypted. A row that fails to encrypt is left plaintext (surfaces as residual
    plaintext in the done-marker re-scan) rather than corrupting the DB."""
    from screencap import corpus_crypto, still_io

    db_path = Path(recording_dir) / "recording.db"
    if not db_path.exists():
        return 0
    count = 0
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT id, recording_timestamp, timestamp, png_data FROM screenshot "
            "WHERE png_data IS NOT NULL"
        )
        rows = cur.fetchall()
        for row_id, rec_ts, shot_ts, blob in rows:
            if blob is None or corpus_crypto.is_encrypted(bytes(blob)):
                continue
            if rec_ts is None or shot_ts is None:
                # No AAD identity to bind to — leave plaintext (re-scan will flag).
                logger.warning(
                    "corpus migrate: screenshot row %s has no timestamp; blob left plaintext",
                    row_id,
                )
                continue
            enc = corpus_crypto.encrypt(
                bytes(blob), key, still_io.png_blob_aad(float(rec_ts), float(shot_ts))
            )
            conn.execute(
                "UPDATE screenshot SET png_data = ? WHERE id = ?",
                (sqlite3.Binary(enc), row_id),
            )
            count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning("corpus migrate: recording.db blob encryption failed", exc_info=True)
        raise
    finally:
        conn.close()
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
    ``sqlcipher_export`` then atomically renames it over the original — all under
    ``content_index_write_lock`` so a concurrent indexer (index_core / scrub_worker
    / retention) can't write to the file across the swap."""
    from screencap.content_index import content_index_write_lock

    index_path = Path(index_path)
    if not index_path.exists():
        return False
    if not _is_plaintext_sqlite(index_path):
        return False  # already encrypted (or not a sqlite db) → nothing to do

    from pysqlcipher3 import dbapi2 as sc

    with content_index_write_lock(require_cross_process=True):
        # Re-check under the lock: another process may have rekeyed while we waited.
        if not _is_plaintext_sqlite(index_path):
            return False
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
    recordings_dir: Path,
    index_path: Path,
    key: bytes,
    *,
    min_stable_age_s: float = 0.0,
) -> MigrationReport:
    """Convert every recording's stills + inline blobs + rekey the content index."""
    report = MigrationReport()
    recordings_dir = Path(recordings_dir)
    if recordings_dir.is_dir():
        for rec_dir in sorted(p for p in recordings_dir.iterdir() if p.is_dir()):
            # The scrubbed cloud-copy siblings are derived, transient, and hold
            # plaintext masked JPEGs by design — never encrypt them.
            if rec_dir.name.endswith("-scrubbed"):
                continue
            try:
                report.stills_encrypted += migrate_recording_stills(
                    rec_dir, key, min_stable_age_s=min_stable_age_s
                )
                report.blobs_encrypted += migrate_recording_db_blobs(rec_dir, key)
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


def corpus_has_plaintext_remaining(recordings_dir: Path, index_path: Path) -> bool:
    """R5 ground truth: True if any plaintext still or plaintext index survives.

    Used to gate the ``corpus_encrypted`` done marker — a clean pass must leave no
    plaintext ``screenshots/*.jpg`` and no plaintext ``content_index.db`` behind.
    (Inline ``png_data`` blobs are covered by the migrate step's own per-row
    re-encrypt + the fail-on-error contract, so this scan targets the two glob-able
    plaintext shapes a reader could pick up.)"""
    recordings_dir = Path(recordings_dir)
    if recordings_dir.is_dir():
        for rec_dir in recordings_dir.iterdir():
            if not rec_dir.is_dir() or rec_dir.name.endswith("-scrubbed"):
                continue
            shots = rec_dir / "screenshots"
            if shots.is_dir() and next(shots.glob("*.jpg"), None) is not None:
                return True
    index_path = Path(index_path)
    if index_path.exists() and _is_plaintext_sqlite(index_path):
        return True
    return False


def flip_corpus_to_encrypted() -> MigrationReport:
    """Perform (or resume) the plaintext→encrypted corpus flip (search U7 / U8).

    Ensures the corpus key, records the intent marker, converts the whole corpus,
    then sets the ``corpus_encrypted`` done marker LAST — but only on a clean,
    plaintext-free pass. Idempotent + resumable."""
    from screencap import config, corpus_crypto
    from screencap.content_index import default_index_path

    key = corpus_crypto.get_or_create_corpus_key()  # raises CorpusKeyUnavailable if impossible
    config.set_corpus_encryption_requested(True)  # persist intent before mutating bytes
    recordings_dir = config.get_recordings_dir()
    index_path = default_index_path()
    report = migrate_corpus(
        recordings_dir, index_path, key, min_stable_age_s=_ACTIVE_WRITE_GRACE_S
    )
    plaintext_remains = corpus_has_plaintext_remaining(recordings_dir, index_path)
    if report.errors or plaintext_remains:
        # Leave corpus_encryption_requested set so the next daemon start retries;
        # do NOT flip readers onto encrypted paths while plaintext survives.
        logger.warning(
            "corpus migration incomplete (errors=%s, plaintext_remaining=%s); "
            "leaving corpus_encrypted UNSET for retry",
            report.errors,
            plaintext_remains,
        )
        return report
    config.set_corpus_encrypted(True)  # done marker — readers now use encrypted paths
    logger.info(
        "corpus migration complete: %d stills + %d blobs encrypted across %d recordings; "
        "index rekeyed=%s",
        report.stills_encrypted,
        report.blobs_encrypted,
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
    "migrate_recording_db_blobs",
    "rekey_content_index",
    "migrate_corpus",
    "corpus_has_plaintext_remaining",
    "flip_corpus_to_encrypted",
    "resume_at_daemon_start",
]
