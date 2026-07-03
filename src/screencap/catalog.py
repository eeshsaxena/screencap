"""Scan recordings directory and load metadata."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple

from screencap.config import get_recordings_dir
from screencap.recording_db import has_column, has_table, open_recording_db

logger = logging.getLogger(__name__)

INTENT_FILE = ".recording_intent"

# SCR-116: the cloud account uid that OWNS a recording, pinned at start. A
# dotfile, so ``upload.list_recording_files`` (which skips ``.``-prefixed names)
# never uploads it — this is local-only ownership metadata.
OWNER_UID_FILE = ".cloud_owner_uid"


def write_owner_uid(directory: Path, uid: str) -> None:
    """Pin the cloud account uid that owns this recording (SCR-116).

    Written once by the daemon at token-staging time — the daemon is the only
    component that can read the Keychain / mint the token, so it is the only one
    that knows the uid. The terminal stage reads it back to refuse converging a
    recording into a *different* account's namespace after a mid-lifecycle
    account switch (``screencap logout && login`` as another user).
    """
    (directory / OWNER_UID_FILE).write_text(uid)


def read_owner_uid(directory: Path) -> str | None:
    """Read the pinned owner uid, or ``None`` when absent.

    ``None`` for a legacy recording (predating the pin) or a local recording
    (never tied to an account) — callers treat that as "ownership unknown" and
    fall back to their existing behavior rather than refusing.
    """
    try:
        uid = (directory / OWNER_UID_FILE).read_text().strip()
    except (OSError, ValueError):
        return None
    return uid or None


def read_upload_warning(directory: Path) -> str | None:
    """Best-effort deferred-upload follow-up text from ``.upload_followup.json``.

    Returns the human-readable warning for a cloud recording whose upload did not
    fully complete at finalize and was handed to the daemon resume / manual
    ``screencap upload`` (the ``upload_disabled`` follow-up kind carries text;
    the others typically leave it ``None``). ``None`` when the file is absent,
    unreadable, or carries no warning text.

    CAVEAT (SCR-148): this is best-effort context, NOT a durable upload-state
    oracle. The file is EPHEMERAL — ``recorder.print_upload_followup`` deletes it
    after the session controller prints it — and it never carries the SCR-116
    *account-mismatch* refusal, which is set only on the in-memory
    ``terminal_stage.TerminalStageResult``. For the durable account-mismatch
    signal, read :func:`read_owner_uid` and compare it against the currently
    signed-in uid (``auth.whoami`` / the daemon's ``/v0/auth.whoami`` verb).
    """
    path = directory / ".upload_followup.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    warning = data.get("upload_warning") if isinstance(data, dict) else None
    return warning if isinstance(warning, str) and warning else None


def read_intent(directory: Path) -> str | None:
    """Read the recording intent from a .recording_intent file.

    Returns 'cloud', 'local', 'both', or None if the file is missing/corrupt.
    """
    intent_path = directory / INTENT_FILE
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
        return data.get("destination")
    except Exception:
        return None


def read_masked_video_upload(directory: Path) -> bool | None:
    """Read the FROZEN ``masked_video_upload`` decision from ``.recording_intent``.

    The masked-video-upload flag is resolved ONCE at recording start and frozen
    into ``.recording_intent`` (``engine/lock_policy._write_identity_files``), so
    a mid-recording flip of the mutable global cannot make capture-time blocking
    and the live/terminal upload-time masking disagree (the rich-video leak
    window — SCR-125 R-SCR125-A). Both the live ``chunk_processor`` upload and the
    ``terminal_stage`` masking read THIS frozen value, never the global.

    Returns the frozen bool, or ``None`` when the file is missing/corrupt or the
    field is absent (a legacy intent written before SCR-125). The caller falls
    back to the mutable global only for those legacy recordings — see
    ``pipeline_chunk_ops.get_frozen_masked_video_upload``.
    """
    intent_path = directory / INTENT_FILE
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
    except Exception:
        return None
    val = data.get("masked_video_upload")
    return bool(val) if isinstance(val, bool) else None


def read_intent_privacy_mode(directory: Path) -> str | None:
    """Read the FROZEN capture-time ``privacy_mode`` from ``.recording_intent``.

    The capture-time ``PrivacyMode`` is resolved once at recording start and
    frozen here (``engine/lock_policy._write_identity_files``, from
    ``privacy_config.mode.value``). The SCR-178 backfill reads THIS value to
    re-derive a ``local`` recording's skip set under the mode it was captured
    under, so a *relaxed-since-capture* global mode can never make the backfill
    block less than capture-time did (SCR-190).

    Returns the frozen mode string (e.g. ``"public"`` / ``"internal"``), or
    ``None`` when the file is missing/corrupt or the field is absent (a legacy
    intent written before the field existed). The caller fails closed to
    ``PrivacyMode.PUBLIC`` for that ``None`` case rather than trusting the
    mutable global.
    """
    intent_path = directory / INTENT_FILE
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
    except Exception:
        return None
    val = data.get("privacy_mode")
    return val if isinstance(val, str) and val else None


def read_intent_policy(directory: Path):
    """Read the frozen :class:`~screencap.pipeline_policy.ResolvedPolicy`.

    Returns the policy frozen into ``.recording_intent`` at routing time
    (U3), or ``None`` when the file is missing/corrupt OR is a legacy
    ``version: 1`` intent written before the retention fields existed
    (sparse-but-valid — the caller should fall back to a config default or
    treat retention as keep-forever). The frozen value is authoritative: it
    is NOT re-resolved against current config, so a config change after the
    recording was routed does not change this recording's policy.
    """
    intent_path = directory / INTENT_FILE
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
    except Exception:
        return None
    from screencap.pipeline_policy import ResolvedPolicy

    return ResolvedPolicy.from_dict(data)


class RecordingInfo(NamedTuple):
    name: str
    date: str  # YYYY-MM-DD
    duration: str  # e.g. "2m 34s"
    size_mb: str  # e.g. "48.3 MB"
    has_audio: bool
    transcribed: bool
    uploaded: bool
    drops: dict[str, int] | None = None  # event drop counts, if any
    is_stub: bool = False  # True if media files deleted after upload
    chunks_total: int = 0  # number of video chunks (0 = legacy single-file)
    chunks_uploaded: int = 0  # number of chunks with upload status files
    intent: str | None = None  # "cloud", "local", or None (legacy)
    # R14 (U9): True for a chunked recording (the unified pipeline's canonical
    # on-disk shape going forward), False for a legacy single-file recording.
    # Survives stubbing — derived from chunk videos, manifests, AND legacy
    # status files, so an uploaded-and-evicted chunked recording (no local
    # chunk_*.mp4 left) is still flagged chunked. Consumers (review/viewer,
    # SwiftUI) read this to choose the chunked vs. legacy single-file path.
    is_chunked: bool = False
    # Raw values for SwiftUI consumers (Unit 4c). The pre-formatted ``date``
    # / ``duration`` strings remain for backward compatibility with anything
    # that reads the existing JSON; SwiftUI uses these unformatted fields
    # for HH:MM rendering, sorting, and date-bucket grouping.
    started_at: float | None = None  # Unix timestamp from the recording row
    duration_seconds: float | None = None  # raw seconds, source of `duration`
    # SCR-148: cloud account-mismatch observability (a SCR-116 follow-up). Both
    # are mirrored EXACTLY onto daemon.schema.RecordingSummary — recording.list
    # asserts the two field sets match, so any field added here must be added
    # there too. `owner_uid` is the Firebase uid pinned at start (.cloud_owner_uid,
    # local-only); None for legacy/local recordings. An agent compares it against
    # /v0/auth.whoami to tell a permanent account-mismatch block apart from a
    # transient upload failure. `upload_warning` is best-effort follow-up text —
    # see read_upload_warning for why it is NOT the account-mismatch signal.
    owner_uid: str | None = None
    upload_warning: str | None = None
    # U2 (prototype UI): additive fields the new SwiftUI surfaces render directly,
    # so Library / Journal / HUD / sidebar footer need no client-side string
    # parsing. Mirrored EXACTLY onto daemon.schema.RecordingSummary — recording.list
    # asserts the two field sets match, so any field added here must be added there.
    # `size_bytes` is the numeric total behind the formatted `size_mb` (sidebar
    # footer sums it). `summary` is the namer's `recording.task_description` (null
    # when absent or the DB is locked). `title` is the humanized directory name (the
    # namer's slug) shown on cards/HUD — a *title*, distinct from the `summary`
    # description. `state` is the derived lifecycle (`recording`|`processing`|`ready`,
    # KTD-7). `recording_id` is the stable id pinned at start (survives the post-stop
    # auto-name rename) so clients hold identity across it.
    size_bytes: int = 0
    summary: str | None = None
    title: str = ""
    state: Literal["recording", "processing", "ready"] = "ready"
    recording_id: str | None = None


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _dir_size_bytes(p: Path) -> int:
    """Total bytes of every file under ``p`` — the numeric source of ``size_mb``."""
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _fmt_size(total_bytes: int) -> str:
    if total_bytes < 1024 * 1024:
        return f"{total_bytes / 1024:.1f} KB"
    return f"{total_bytes / (1024 * 1024):.1f} MB"


def _dir_size_mb(p: Path) -> str:
    return _fmt_size(_dir_size_bytes(p))


def _humanize_name(name: str) -> str:
    """Humanize the namer's slug/directory name into a display title (U2).

    The auto-named recording directory is a kebab-case slug
    (``namer.validate_slug``); turn it into Title Case for the card/HUD title
    (``"stripe-webhook-debugging"`` → ``"Stripe Webhook Debugging"``). Internal
    capitals are preserved (``.capitalize`` would lowercase them). A legacy
    timestamp-named recording passes its stamp through with separators spaced —
    an acceptable fallback for a recording the namer never renamed.
    """
    words = name.replace("_", " ").replace("-", " ").split()
    if not words:
        return name
    return " ".join(w[:1].upper() + w[1:] for w in words)


def read_recording_id(directory: Path) -> str | None:
    """Read the stable ``.recording_id`` — the name pinned at recording start.

    Written once by the engine at start (``engine/lock_policy``) into the capture
    dir, so it moves with the directory and survives the post-stop auto-name
    rename: a stable identity clients hold across that rename (U2). ``None`` for a
    legacy recording predating the sidecar (callers fall back to the directory
    name).
    """
    try:
        rid = (directory / ".recording_id").read_text().strip()
    except (OSError, ValueError):
        return None
    return rid or None


def _read_task_description(db_path: Path) -> str | None:
    """Best-effort read of ``recording.task_description`` — the namer's summary.

    Lock-tolerant like :func:`_read_recording_meta`: returns ``None`` (never
    raises) when the DB is absent, locked by an active recording, corrupt, or the
    column/value is missing, so a locked DB yields a null summary rather than an
    exception (the review-data nullable-timing contract).
    """
    import sqlite3

    try:
        with open_recording_db(db_path, busy_timeout_ms=500) as conn:
            if has_table(conn, "recording") and has_column(
                conn, "recording", "task_description"
            ):
                row = conn.execute(
                    "SELECT task_description FROM recording LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    val = str(row[0]).strip()
                    return val or None
            return None
    except (sqlite3.Error, OSError, ValueError):
        return None


def _active_recording_name() -> str | None:
    """Directory name of the currently-active recording, or ``None`` (U2).

    Disk-only — reads the pidfile lock, no daemon round-trip — so both the CLI
    (``screencap list``) and the daemon's ``recording.list`` derive the same
    ``recording`` state. Returns the ``recording_name`` from the lock metadata iff
    the lock is actively held by a live process.
    """
    from screencap import pidfile

    try:
        if not pidfile.lock_is_active():
            return None
        meta = pidfile.read_lock_metadata()
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    name = meta.get("recording_name")
    return name if isinstance(name, str) and name else None


class _LedgerProbe(NamedTuple):
    """READ-ONLY snapshot of a recording's ``pipeline_chunk_state`` ledger (U2)."""

    # ≥1 chunk confirmed UPLOADED. An EVICTED chunk keeps its UPLOADED upload_state
    # per the U1 contract, so an uploaded-then-evicted recording still reads True.
    has_uploaded: bool
    # ≥1 chunk FAILED — blocks the completeness sentinel forever, so state must
    # fall to `ready` rather than eternal `processing`.
    has_failed: bool
    # Chunks in a terminal "done" lifecycle (local_done/uploaded/evicted/skipped),
    # mirroring PipelineLedger.all_complete.
    done_count: int
    # The FROZEN closed-set count, or None when not yet frozen.
    chunks_expected: int | None


def _ledger_probe(db_path: Path) -> _LedgerProbe | None:
    """READ-ONLY probe of the U1 pipeline ledger — one open, everything the
    catalog needs (the ``uploaded`` flag AND KTD-7 state derivation).

    Returns ``None`` when there is no ledger to consult — the table does not exist
    (legacy / not-yet-seeded recording) or the DB is unreadable — so callers fall
    back to the file-presence heuristics (R14). Opens ``mode=ro`` (not
    ``open_recording_db``) and never migrates the schema: catalog listing must
    never ALTER a recording.db (that would change its content hash and defeat the
    scrubbed-copy reuse check on the upload path, mirroring
    ``terminal_stage._open_ledger_readonly``). ``mode=ro`` is also required for the
    post-upload/evicted state where the ``-wal``/``-shm`` sidecars are gone.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        if not has_table(conn, "pipeline_chunk_state"):
            return None  # no ledger — fall back to file-presence heuristics.
        has_uploaded = (
            conn.execute(
                "SELECT 1 FROM pipeline_chunk_state "
                "WHERE upload_state='uploaded' LIMIT 1"
            ).fetchone()
            is not None
        )
        has_failed = (
            conn.execute(
                "SELECT 1 FROM pipeline_chunk_state "
                "WHERE lifecycle='failed' OR upload_state='failed' LIMIT 1"
            ).fetchone()
            is not None
        )
        done_count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_chunk_state "
            "WHERE lifecycle IN ('local_done','uploaded','evicted','skipped')"
        ).fetchone()[0]
        expected: int | None = None
        try:
            row = conn.execute(
                "SELECT chunks_expected FROM recording LIMIT 1"
            ).fetchone()
            if row is not None and row[0] is not None:
                expected = int(row[0])
        except sqlite3.Error:
            expected = None
        return _LedgerProbe(has_uploaded, has_failed, int(done_count), expected)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _derive_state(
    directory: Path,
    *,
    is_active: bool,
    is_stub: bool,
    intent: str | None,
    probe: _LedgerProbe | None,
) -> Literal["recording", "processing", "ready"]:
    """Derive the recording's lifecycle state per KTD-7.

    ``recording`` while the pidfile lock names this dir; after stop, route by the
    FROZEN destination: cloud/both recordings complete on the on-disk completeness
    sentinel (``recording_complete.json`` — the terminal stage's last write), local
    recordings on the ledger gate (all frozen ``chunks_expected`` chunks reached a
    terminal "done" lifecycle — the LOCAL route never writes the sentinel, so it is
    deliberately NOT consulted for local). A FAILED chunk, a legacy recording (no
    ledger), or an uploaded-then-evicted stub map to ``ready`` rather than eternal
    ``processing``. ``probe`` is the recording's :func:`_ledger_probe` result,
    read once by the caller.
    """
    if is_active:
        return "recording"

    # A FAILED chunk blocks completion forever — never sit in eternal processing.
    if probe is not None and probe.has_failed:
        return "ready"

    if intent in ("cloud", "both"):
        if (directory / "recording_complete.json").exists():
            return "ready"
        # Uploaded-then-evicted: unambiguously done even if the local sentinel was
        # cleaned away with the media.
        if is_stub:
            return "ready"
        # `processing` requires a real path to the completeness sentinel — a seeded
        # ledger with a FROZEN `chunks_expected` the terminal stage can gate on.
        # Neither a missing ledger nor an unfrozen `chunks_expected` can ever
        # produce a sentinel, so both map to `ready` rather than eternal
        # `processing` (mirrors the local branch's unfrozen-count guard below).
        if probe is None or probe.chunks_expected is None:
            return "ready"
        return "processing"

    # local or legacy(None): the ledger gate over the frozen closed set.
    if probe is None:
        return "ready"  # no ledger — legacy / not seeded
    if probe.chunks_expected is None:
        return "ready"  # no frozen count — don't sit in eternal processing
    return "ready" if probe.done_count >= probe.chunks_expected else "processing"


def read_drops(directory: Path) -> dict[str, int] | None:
    """Read event drop counts from profiling.json, if present."""
    profiling = directory / "profiling.json"
    if not profiling.exists():
        return None
    try:
        data = json.loads(profiling.read_text())
        drops = data.get("drops")
        if isinstance(drops, dict) and any(v > 0 for v in drops.values()):
            return drops
    except Exception:
        pass
    return None


def find_db(directory: Path) -> Path | None:
    """Find the recording.db in a recording directory."""
    p = directory / "recording.db"
    if p.exists():
        return p
    return None


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    """Return whether an ``OperationalError`` is a transient SQLite lock/busy.

    Prefers the numeric error code (Python 3.11+ sets ``sqlite_errorcode`` on
    raised exceptions); falls back to message matching on 3.10, or for
    manually-constructed exceptions that carry no code. SQLite raises
    SQLITE_BUSY (5) / SQLITE_LOCKED (6) — and their extended variants, which
    share the low byte — for lock contention; other ``OperationalError``\\ s
    (disk I/O, missing table) are not transient locks (SCR-166).
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _read_recording_meta(
    db_path: Path,
) -> tuple[float | None, float | None, Literal["ok", "locked", "corrupt"]]:
    """Read (started_timestamp, duration_seconds, timing_status) from a recording.db.

    ``timing_status`` is one of:

    * ``"ok"`` — the read succeeded. A DB that opens cleanly but carries no
      usable timing (no ``recording`` table, a zero/NULL timestamp, or no
      ``action_event`` rows) is still ``"ok"`` with ``None`` timing, so a
      legitimately event-free recording is never flagged.
    * ``"locked"`` — the DB was locked/busy: an active recording (or a
      concurrent writer) held it past ``busy_timeout``, raising
      ``sqlite3.OperationalError``. A *transient* condition that resolves once
      the writer releases — NOT corruption (SCR-166).
    * ``"corrupt"`` — the read failed for any other reason (a corrupt,
      truncated, or otherwise unreadable DB; an OS/parse error).

    Display-only callers (``list_recordings``, ``cli info``) ignore the third
    value; the review-data emitter threads it so the UI can tell a *transient
    lock* ("timeline temporarily unavailable") from *corruption* ("metadata
    couldn't be read") from a *benign event-free* recording (no advisory) —
    without it, ``except Exception`` collapses all three into the same
    ``(None, None)`` (SCR-107 split corrupt from event-free; SCR-166 splits
    locked from corrupt).
    """
    import sqlite3

    try:
        # SCR-166: fail fast (~500ms) on a locked DB rather than blocking the
        # full 5s default — the lock is then classified below, not waited out.
        with open_recording_db(db_path, busy_timeout_ms=500) as conn:
            started: float | None = None
            duration: float | None = None

            if has_table(conn, "recording"):
                row = conn.execute("SELECT timestamp FROM recording LIMIT 1").fetchone()
                started = float(row[0]) if row and row[0] else None
                if started and has_table(conn, "action_event"):
                    ev = conn.execute("SELECT MAX(timestamp) FROM action_event").fetchone()
                    if ev and ev[0] is not None:
                        duration = float(ev[0]) - started

            return started, duration, "ok"
    except sqlite3.OperationalError as exc:
        # OperationalError ⊂ DatabaseError ⊂ sqlite3.Error, so this clause must
        # precede the broad catch below. A lock/busy is transient; everything
        # else (disk I/O, etc.) is treated as an unreadable DB.
        if _is_lock_error(exc):
            logger.warning("recording.db locked (transient) for %s: %r", db_path, exc)
            return None, None, "locked"
        logger.warning("recording.db read failed for %s: %r", db_path, exc)
        return None, None, "corrupt"
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("recording.db read failed for %s: %r", db_path, exc)
        return None, None, "corrupt"


def get_seen_bundle_ids(directories: list[Path] | None = None) -> set[str]:
    """Collect distinct app_bundle_id values from recording DBs.

    Args:
        directories: Specific recording dirs to scan. If None, scans all
            recordings in the configured recordings dir.

    Returns:
        Set of bundle ID strings seen across all scanned recordings.
    """
    if directories is None:
        rec_dir = get_recordings_dir()
        if not rec_dir.exists():
            return set()
        directories = [d for d in rec_dir.iterdir() if d.is_dir()]

    seen: set[str] = set()
    for d in directories:
        db_path = find_db(d)
        if db_path is None:
            continue
        try:
            with open_recording_db(db_path) as conn:
                if has_table(conn, "window_event"):
                    rows = conn.execute(
                        "SELECT DISTINCT app_bundle_id FROM window_event "
                        "WHERE app_bundle_id IS NOT NULL AND app_bundle_id != ''"
                    ).fetchall()
                    seen.update(r[0] for r in rows)
        except Exception:
            continue
    return seen


def list_recordings(recordings_dir: Path | None = None) -> list[RecordingInfo]:
    """Scan recordings directory and return metadata for each."""
    if recordings_dir is None:
        recordings_dir = get_recordings_dir()

    results = []
    if not recordings_dir.exists():
        return results

    # Disk-only active-recording probe, read once for the whole scan (KTD-7).
    active_name = _active_recording_name()

    for d in sorted(recordings_dir.iterdir()):
        if not d.is_dir():
            continue
        db = find_db(d)
        if db is None:
            continue

        started, duration, _ = _read_recording_meta(db)
        date_str = "—"
        if started:
            date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d")

        has_audio = (d / "audio.flac").exists() or any(d.glob("audio_*.flac"))
        transcribed = (d / "transcript.txt").exists() or any(d.glob("transcript_*.txt"))
        uploaded_legacy = (d / ".upload_status.json").is_file()

        # Chunk detection — count from local files, status files, and manifests
        chunk_videos = sorted(d.glob("chunk_*.mp4"))
        chunk_status_files = list(d.glob(".chunk_*_status.json"))
        chunk_manifests = sorted(d.glob("chunk_*_manifest.json"))
        chunks_uploaded = len(chunk_status_files)
        # chunks_total: use max of local videos, manifests, and uploaded count
        # (local files may be deleted after upload)
        chunks_total = max(len(chunk_videos), len(chunk_manifests), chunks_uploaded)
        # R14 (U9): chunked vs. legacy single-file. Derived from the same
        # eviction-surviving evidence as chunks_total, so a stubbed chunked
        # recording (no chunk_*.mp4 left, but manifests/status files remain)
        # is still flagged chunked. A legacy single-file recording has none of
        # these -> is_chunked False and stays listable/readable via video.mp4.
        is_chunked = chunks_total > 0

        # Consult the U1 ledger (read-only) once — one open serves both the
        # `uploaded` flag and the KTD-7 `state` gate. The unified pipeline uploads
        # from the source dir and records confirmed uploads in
        # `pipeline_chunk_state`, so a chunked recording can be genuinely uploaded
        # (and later evicted) WITHOUT the legacy `.chunk_*_status.json` markers the
        # file heuristics key off. `None` = no ledger (legacy / not seeded) -> fall
        # back to file-presence only (R14).
        ledger = _ledger_probe(db)
        ledger_uploaded = bool(ledger and ledger.has_uploaded)

        uploaded = uploaded_legacy or chunks_uploaded > 0 or ledger_uploaded

        # For stubbed recordings, check chunk status files for audio evidence
        if not has_audio and chunks_uploaded > 0:
            for sf in chunk_status_files:
                try:
                    data = json.loads(sf.read_text())
                    if any("audio_" in f for f in data.get("files", [])):
                        has_audio = True
                        break
                except Exception:
                    pass

        # Stub detection: DB exists but no media files on disk. Hidden mp4/flac
        # are ignored — pathlib glob matches dotfiles, and a lingering review
        # artifact (.video_review.mp4) must never mask a stub (R6). A real
        # video.mp4 is non-hidden, so the *.mp4 glob already covers it.
        #
        # Derived from CHUNKS (R2), never from a single `video.mp4` being the
        # record: the `*.mp4` glob counts surviving `chunk_*.mp4` as media, so a
        # chunked recording with chunks still on disk is correctly NOT a stub
        # even though it has no merged `video.mp4` (that is a derived on-demand
        # artifact — see `viewer._ensure_single_video`).
        #
        # Ledger-aware (U10): an uploaded chunked recording whose media was
        # evicted post-upload-confirm is a LEGITIMATE stub. Folding
        # `ledger_uploaded` into `uploaded` above makes the unified pipeline's
        # uploaded-then-evicted recording read as uploaded even without legacy
        # `.chunk_*_status.json` markers, so the `uploaded and not has_media`
        # rule below classifies it correctly. The ledger NEVER manufactures a
        # FALSE stub: a FAILED chunk (fail-closed) is never UPLOADED so it cannot
        # set `ledger_uploaded`, and a locally-evicted recording (LOCAL_DONE ->
        # EVICTED, never uploaded) keeps `uploaded == False` — neither is a stub.
        has_media = (
            any(not p.name.startswith(".") for p in d.glob("*.mp4"))
            or any(not p.name.startswith(".") for p in d.glob("*.flac"))
        )
        is_stub = uploaded and not has_media and db is not None

        drops = read_drops(d)
        intent = read_intent(d)
        # SCR-148: both are pure local reads (no auth / Keychain access), so
        # list_recordings stays a cheap local scan usable without sign-in.
        owner_uid = read_owner_uid(d)
        upload_warning = read_upload_warning(d)

        # U2 additive fields. Compute bytes once and format from it (was two
        # rglob passes); derive the lifecycle state from the active-session
        # probe + destination-routed completion gate (KTD-7), reusing the single
        # ledger probe read above.
        total_bytes = _dir_size_bytes(d)
        is_active = d.name == active_name
        state = _derive_state(
            d,
            is_active=is_active,
            is_stub=is_stub,
            intent=intent,
            probe=ledger,
        )
        # The namer writes `task_description` only AFTER stop, and the active
        # recording holds a write lock — so reading it here can't yield a summary
        # and would just block up to the 500ms busy_timeout on every scan. Skip it.
        summary = None if is_active else _read_task_description(db)

        results.append(
            RecordingInfo(
                name=d.name,
                date=date_str,
                duration=_fmt_duration(duration),
                size_mb=_fmt_size(total_bytes),
                has_audio=has_audio,
                transcribed=transcribed,
                uploaded=uploaded,
                drops=drops,
                is_stub=is_stub,
                chunks_total=chunks_total,
                chunks_uploaded=chunks_uploaded,
                intent=intent,
                is_chunked=is_chunked,
                started_at=started,
                duration_seconds=duration,
                owner_uid=owner_uid,
                upload_warning=upload_warning,
                size_bytes=total_bytes,
                summary=summary,
                title=_humanize_name(d.name),
                state=state,
                recording_id=read_recording_id(d),
            )
        )

    return results
