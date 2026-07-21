"""Global FTS5 content-index sidecar over post-OCR on-screen text.

This module owns ``~/.screencap/content_index.db`` — a single, global SQLite
sidecar that makes on-screen text searchable across every recording. It is the
storage half of the MCP agent-memory retrieval surface (SCR-118); the OCR pass
that *feeds* it lives in ``chunk_processor`` (U2) and the read verbs that *query*
it live in ``daemon/app.py`` (U3).

Why a global sidecar (not a table inside each ``recording.db``)
--------------------------------------------------------------
Cross-recording queries ("what did I see this morning") need one store, and the
per-recording ``recording.id`` is not globally unique (each ``recording.db`` has
its own id space). So the global key is the recording **directory name** plus the
frame ``timestamp_ms``. Co-locating this text inside ``recording.db`` would also
mix trust boundaries — that file is deliberately never uploaded — and fight the
engine's SQLAlchemy-owned schema. A standalone sidecar additionally survives
``stub_recording`` so a recording stays queryable after its media is deleted.

Sensitivity & the narrowed R7 (SCR-118)
---------------------------------------
The index holds on-screen text recognised from **local, unmasked** screenshots
(the live recorder never masks screenshots in place; masking only ever runs over
a ``<name>-scrubbed`` *copy* at the terminal/cloud stage). The index is therefore
the same sensitivity class as the recordings themselves. Two guarantees carry the
privacy weight that "only index redacted frames" was meant to provide:

1. **Never uploaded** — like ``recording.db``, this file stays on the machine
   (``upload`` excludes it; it lives outside the ``recordings/`` tree entirely).
2. **Purged on destroy** — :meth:`delete_recording` /
   :meth:`delete_recording_interval` remove rows when a recording is deleted or
   an app is retroactively disabled, so content the user destroyed stops being
   queryable.

The U2 feeder only indexes frames the privacy policy classified ``ALLOW`` — it
skips every frame inside a flagged interval (secure-field, EXCLUDE, MASK_WINDOW,
etc.), so focused password fields and masked windows are not indexed. See
``SECURITY.md``.

Hardened at-rest perms
----------------------
``sqlite3.connect`` follows symlinks and creates files at the umask-derived mode
(typically ``0o644``); the ``-wal``/``-shm`` sidecars are created by SQLite's C
internals with no ``O_NOFOLLOW`` path. So we (1) reject a symlinked DB path or
parent before connecting, (2) ``chmod 0o600`` the DB immediately after connect,
(3) ``chmod 0o600`` the ``-wal``/``-shm`` right after enabling WAL, and (4) keep
the parent dir at ``0o700``. This mirrors the ``auto-serve.log`` precedent in
``cli/_autospawn.py``.

Concurrency
-----------
One writer process at a time (the chunk-processor thread; one active recording),
plus a same-process retroactive ``scrub_worker`` purge, read concurrently by the
daemon. WAL alone is insufficient, so every connection sets ``busy_timeout=10000``
(matching ``scrub_worker``), each chunk's frame batch is written in a single
committed transaction (a reader never sees a half-written chunk), and the writer
checkpoints (``wal_checkpoint(PASSIVE)``) after each commit to bound ``-wal``
growth.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# Snippet/limit budgets. The default/max mirror the daemon's shared query limit
# contract (``daemon.app._QUERY_DEFAULT_LIMIT`` / ``_QUERY_MAX_LIMIT``) so all
# three read verbs honor one limit ceiling — the daemon clamps before calling in,
# and this is the floor for any direct in-process caller.
_SNIPPET_TOKEN_BUDGET = 32
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_BUSY_TIMEOUT_MS = 10000

# Backend-agnostic DB error tuples. ``content_index.db`` is plain ``sqlite3`` by
# default, but SQLCipher-encrypted (via ``pysqlcipher3``) once the corpus is
# migrated (search U4 / R3). pysqlcipher3's exception classes are DISJOINT from
# sqlite3's, so every ``except`` that must also catch an encrypted-backend failure
# references these tuples; :func:`_load_sqlcipher` widens them to include the
# SQLCipher classes the first time an encrypted connection is opened.
_sqlcipher = None  # lazily imported ``pysqlcipher3.dbapi2`` module (None until used)
_DATABASE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.DatabaseError,)
_OPERATIONAL_ERRORS: tuple[type[BaseException], ...] = (sqlite3.OperationalError,)


def _load_sqlcipher():
    """Import ``pysqlcipher3`` once, widening the backend-error tuples to include
    its (sqlite3-disjoint) exception classes.

    Raises :class:`_StoreUnavailable` when the binding is not installed, so an
    encrypted store degrades to ``STORE_UNAVAILABLE`` (fail-closed, R8) on a host
    without the SQLCipher toolchain rather than crashing.
    """
    global _sqlcipher, _DATABASE_ERRORS, _OPERATIONAL_ERRORS
    if _sqlcipher is None:
        try:
            from pysqlcipher3 import dbapi2 as sc
        except ImportError as exc:
            raise _StoreUnavailable(
                "content index is encrypted but the SQLCipher binding "
                "(pysqlcipher3) is unavailable"
            ) from exc
        _sqlcipher = sc
        _DATABASE_ERRORS = (sqlite3.DatabaseError, sc.DatabaseError)
        _OPERATIONAL_ERRORS = (sqlite3.OperationalError, sc.OperationalError)
    return _sqlcipher


class IndexState(str, Enum):
    """Why a search returned what it did — keeps "empty" from being ambiguous.

    A corrupt global store must never be silently indistinguishable from "no
    match", so the read path always reports one of these. ``not_indexed`` is set
    by the daemon caller (no store file yet); the store itself only ever emits
    ``ok``, ``no_match``, ``index_degraded`` (FTS5 absent → LIKE fallback), or
    ``store_unavailable`` (missing/corrupt/symlinked).
    """

    OK = "ok"
    NO_MATCH = "no_match"
    NOT_INDEXED = "not_indexed"
    INDEX_DEGRADED = "index_degraded"
    STORE_UNAVAILABLE = "store_unavailable"


@dataclass(frozen=True)
class SearchHit:
    """A single ranked match: text snippet + locating pointer, never a path.

    ``score`` is the bm25 value (most-negative = best) on the FTS5 path, or
    ``0.0`` on the LIKE fallback where no ranking is available.

    ``match_source`` names the stream a hit came from: ``"content"`` (the default
    — an on-screen-text frame match whose ``timestamp_ms`` is a real frame
    pointer) or ``"title"`` (a user-set recording-title match the daemon unions
    into ``content.search``, whose ``timestamp_ms`` is the sentinel ``0`` — NOT a
    frame pointer). Additive + defaulted so the store's own positional
    construction sites (``SearchHit(rec, ts, snip, score)``) keep working
    unchanged; only the title-union path sets it to ``"title"``.
    """

    recording: str
    timestamp_ms: int
    snippet: str
    score: float
    match_source: str = "content"


@dataclass(frozen=True)
class SearchResult:
    """Result of a content search: ranked hits + the reason for the outcome."""

    hits: list[SearchHit] = field(default_factory=list)
    index_state: IndexState = IndexState.NO_MATCH


@dataclass(frozen=True)
class IndexFrame:
    """One frame's worth of recognised text to persist."""

    timestamp_ms: int
    text: str


# --------------------------------------------------------------------------
# Day-diary block search (SCR day-diary U6 / KTD-8)
# --------------------------------------------------------------------------
#
# A SECOND FTS surface inside the SAME ``content_index.db``: block name + topic
# bullet text keyed by ``(recording, block_id)`` with the block's ms span, so a
# fuzzy topic memory ("kick", "design systems") finds a diary BLOCK across months
# of history in one query (R5). It rides the same machinery as ``content_fts`` —
# the FTS5 probe + escaped-``LIKE`` fallback, the hardened perms, the
# ``content_index_write_lock()``, the WAL + ``busy_timeout`` + PASSIVE checkpoint
# — but is otherwise independent:
#
# * **Default-on (P2 / R5).** The diary write is INDEPENDENT of the default-off
#   ``content_index_enabled`` flag (which gates only the OCR ``content_fts``
#   pass). Block name/bullet text is always written after consolidation; the DB
#   file/dir is created with the same hardened perms whether or not OCR indexing
#   is on.
# * **Narrative is NEVER indexed** (KTD-8) — only block name + bullet text.
# * **Purge-race safe (P1).** :func:`write_recording_diary` holds
#   ``content_index_write_lock()`` and RE-READS the recording's block rows under
#   that lock immediately before writing — the diary equivalent of the
#   ``index_core`` unlink-before-write re-``stat`` barrier. A consolidation pass
#   that read block rows BEFORE a retroactive-disable purge deleted them must not
#   re-insert the disabled app's text after the purge's diary delete; re-reading
#   under the lock closes that window. The purge side (U5) deletes diary rows
#   under the SAME lock via :meth:`ContentIndex.delete_recording_diary_interval`.


@dataclass(frozen=True)
class DiaryEntry:
    """One diary block's searchable text + locating pointer, to persist.

    ``text`` is the block NAME plus its topic bullets joined (never the narrative
    — KTD-8). ``start_ms`` / ``end_ms`` are the block's absolute unix-ms span
    (the recall surface's ms convention, matching ``timestamp_ms``); the interval
    purge (U5) keys on them.
    """

    block_id: str
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class DiaryHit:
    """A single ranked diary match: text snippet + a POINTER, never a path.

    Pointer-only (R5 / KTD-8): ``(recording, block_id)`` plus the block's ms span
    — the app deep-links by ``block_id`` and locates the block in its day by span,
    never receiving full bullet text beyond the matched ``snippet``. ``score`` is
    the bm25 value (most-negative = best) on the FTS5 path, or ``0.0`` on the LIKE
    fallback where no ranking is available.
    """

    recording: str
    block_id: str
    start_ms: int
    end_ms: int
    snippet: str
    score: float


@dataclass(frozen=True)
class DiarySearchResult:
    """Result of a diary search: ranked hits + the reason for the outcome."""

    hits: list[DiaryHit] = field(default_factory=list)
    index_state: IndexState = IndexState.NO_MATCH


def default_index_path() -> Path:
    """Return the content-index DB path (does not create the DB file).

    Resolved through the container-aware sidecar chokepoint
    (:func:`screencap.config.get_store_dir`, SCR-236 U3):
    ``~/.screencap/content_index.db`` today (flag off / ``SCREENCAP_RECORDINGS_DIR``
    override), ``<recordings mountpoint>/.store/content_index.db`` when the
    at-rest container is active."""
    from screencap.config import get_store_dir

    return get_store_dir() / "content_index.db"


# SCR-134: serialize content-index writers against the retroactive-disable purge.
# The inline index write (``chunk_processor._do_index_chunk_content``) and the
# purge (``scrub_worker._purge_content_index_intervals``) run on two threads of
# the SAME recorder process. Without serialization an inline write that already
# OCR'd a now-disabled app's frames can commit just AFTER a concurrent purge,
# resurrecting just-purged on-screen text in the global store — and the only
# documented recovery (the user re-disabling the app) never re-purges the index,
# because a re-disable derives an EMPTY interval set from the already-deleted
# window_event rows. Holding this lock across the inline OCR+write and across the
# purge makes the two orderings both converge to "purged": either the write lands
# first and the purge then deletes it, or the purge runs first (after the scrub
# unlinked the screenshots) and the write re-reads an empty disk and indexes
# nothing.
#
# Mirrors ``terminal_stage.terminal_lock``: an in-process ``threading.Lock``
# FIRST (the real mechanism here — both racers are same-process threads — and it
# holds even on flock-unsupported filesystems), then a best-effort cross-process
# ``fcntl.flock`` for any future cross-process writer (e.g. a full-delete CLI).
# The store is global (one DB for all recordings), so this is a single global
# lock, not per-recording. Fail-open: any lock-setup error degrades to the
# in-process lock only — both callers are themselves fail-open and must never
# break on lock setup. The kernel releases the flock on process death, so a crash
# never wedges it.
_WRITE_LOCK = threading.Lock()


class CrossProcessLockUnavailable(Exception):
    """The cross-process ``fcntl.flock`` could not be acquired (SCR-191).

    Raised by :func:`content_index_write_lock` only when ``require_cross_process``
    is set and the flock cannot be established (no-flock filesystem / sandboxed
    run dir). The in-process lock alone is insufficient for a writer in a
    DIFFERENT process from the recorder (the backfill daemon), so such a writer
    must decline to write rather than degrade to no cross-process serialization.
    """


def _write_lock_path() -> Path:
    return default_index_path().parent / "run" / "content-index.lock"


@contextlib.contextmanager
def content_index_write_lock(*, require_cross_process: bool = False) -> Iterator[None]:
    """Hold the global content-index write lock (SCR-134).

    Acquired by both the inline index write and the retroactive-disable purge so
    the two never interleave. Blocking (it must WAIT for the other writer, not
    fail).

    Two failure postures (SCR-191):

    * **Fail-open (default)** — a flock-setup error degrades to the in-process
      lock only. Correct for the recorder-subprocess racers (live index write vs
      retroactive-disable purge): they are same-process threads, so the
      ``threading.Lock`` is the real serialization and the flock is a bonus.
    * **Fail-closed (``require_cross_process=True``)** — a flock-setup error
      raises :class:`CrossProcessLockUnavailable` instead of yielding. The
      backfill runs in the *daemon* process, a DIFFERENT process from the
      recorder's purge, so the in-process lock does NOT serialize them — only the
      flock does. Rather than write without cross-process protection (which could
      resurrect a just-purged interval), the backfill caller declines the write.
      The flock file is one path on one filesystem, so its support is
      all-or-nothing for every process: when flock works the purge holds it too
      (serialized); when it does not, the backfill simply does not write, so the
      only remaining writers are the recorder's own threads (still serialized by
      ``_WRITE_LOCK``).
    """
    _WRITE_LOCK.acquire()
    fd: int | None = None
    try:
        try:
            lock_path = _write_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(lock_path.parent, 0o700)
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            # Sandboxed/read-only run dir or a filesystem without flock support:
            # the in-process lock still serializes the same-process racers, which
            # is the race that actually exists today.
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                fd = None
            if require_cross_process:
                # A cross-process writer (backfill) cannot rely on the in-process
                # lock — decline rather than write unserialized. Raise here (before
                # yield); the ``finally`` releases ``_WRITE_LOCK`` exactly once as
                # the exception unwinds, and ``fd`` is already None so it is a no-op.
                raise CrossProcessLockUnavailable(
                    "content-index cross-process flock unavailable"
                ) from exc
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)
        _WRITE_LOCK.release()


class _StoreUnavailable(Exception):
    """Internal: the store could not be opened (symlink/corruption)."""


def escape_like(term: str) -> str:
    r"""Escape ``%``/``_``/``\`` so a LIKE pattern matches them literally."""
    return (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _quote_fts_token(token: str) -> str:
    """Quote one token as a literal FTS5 phrase (doubling embedded quotes)."""
    return '"' + token.replace('"', '""') + '"'


def _build_fts_match(query: str) -> str:
    """Build an injection-safe FTS5 ``MATCH`` expression for keyword search.

    Each whitespace-separated token is wrapped as its own literal phrase and the
    phrases are ANDed (FTS5's default for adjacent phrases). Quoting every token
    means ``*``, ``NEAR``, ``col:`` and a stray ``"`` are matched as text and can
    never escape into FTS5 query syntax (the realistic injection vector) — while
    still giving keyword-AND recall rather than a single strict full-phrase
    match. The value is passed as a bound parameter; this only controls how FTS5
    *parses* the bound string.
    """
    tokens = [_quote_fts_token(t) for t in query.split() if t]
    return " ".join(tokens)


class ContentIndex:
    """Owns one connection to the global content-index sidecar.

    An instance is single-threaded (a ``sqlite3.Connection`` is not safe to
    share across threads): create one where you need it — per chunk write on
    the chunk-processor thread, per query inside the daemon's ``to_thread``
    worker — and :meth:`close` it (or use it as a context manager).

    The read path (:meth:`search`) never raises: a missing/corrupt/symlinked
    store surfaces as ``IndexState.STORE_UNAVAILABLE`` so a caller treats it as
    empty rather than 500-ing. Write/delete may raise ``sqlite3`` errors; their
    callers (the fail-open U2 pass, the daemon delete sites) wrap them.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        encrypted: bool | None = None,
        key: bytes | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_index_path()
        # Resolve the at-rest format once, consistently across every callsite: when
        # ``encrypted`` is not given, take the shared corpus marker (search U4). An
        # encrypted store needs the corpus key; load it here (read-only) if the
        # caller did not supply one. A missing key on an encrypted store makes
        # ``_open`` fail closed (STORE_UNAVAILABLE) — it never opens plaintext.
        if encrypted is None:
            from screencap import config

            encrypted = config.get_corpus_encrypted()
        self._encrypted = encrypted
        if encrypted and key is None:
            from screencap import corpus_crypto

            try:
                key = corpus_crypto.load_corpus_key()
            except Exception:  # noqa: BLE001 — any key-load failure = "no key" → fail closed
                logger.warning("content index: failed to load the corpus key", exc_info=True)
                key = None
        self._key = key
        self._conn: sqlite3.Connection | None = None
        self._fts_available = False
        self._available = False
        try:
            self._open()
            self._available = True
        except _StoreUnavailable as exc:
            logger.warning("content index unavailable: %s", exc)
        except _DATABASE_ERRORS as exc:
            logger.warning("content index could not be opened: %s", type(exc).__name__)

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> ContentIndex:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def available(self) -> bool:
        """Whether the store opened cleanly (else search returns STORE_UNAVAILABLE)."""
        return self._available

    @property
    def fts_available(self) -> bool:
        """Whether the FTS5 index is in use (vs the degraded LIKE fallback)."""
        return self._fts_available

    # -- open + schema ---------------------------------------------------

    def _open(self) -> None:
        path = self._db_path
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass

        # Symlink guard: sqlite cannot open with O_NOFOLLOW, so refuse a
        # symlinked DB path or parent before connect (mirrors _open_auto_log).
        if os.path.realpath(str(parent)) != os.path.abspath(str(parent)):
            raise _StoreUnavailable("content-index parent dir resolves through a symlink")
        if path.is_symlink():
            raise _StoreUnavailable("content-index db path is a symlink")

        existed = path.exists()
        conn = self._connect(path)
        # Close the create-to-chmod window before any second reader can open it.
        if not existed:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL sidecars are created by SQLite's C internals at umask mode — lock
        # them down immediately, before a concurrent reader could open them.
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                try:
                    os.chmod(side, 0o600)
                except OSError:
                    pass

        self._conn = conn
        self._fts_available = self._probe_fts5(conn)
        self._create_schema(conn)

    def _connect(self, path: Path):
        """Open the backend connection.

        Plain ``sqlite3`` by default (today's behavior). When the corpus is
        encrypted (search U4 / R3), open via SQLCipher and set the raw 256-bit
        corpus key with ``PRAGMA key`` — an ``x'<hex>'`` full-length key makes
        SQLCipher skip PBKDF2 (the corpus key is already high-entropy). An
        immediate ``sqlite_master`` read forces key verification at open, so a
        wrong/absent key or a plaintext file opened as encrypted fails here (→
        ``_StoreUnavailable`` / a widened DB error) instead of silently later.
        """
        if not self._encrypted:
            return sqlite3.connect(str(path))
        if self._key is None:
            raise _StoreUnavailable(
                "content index is encrypted but the corpus key is unavailable"
            )
        sc = _load_sqlcipher()
        conn = sc.connect(str(path))
        conn.execute(f"PRAGMA key = \"x'{self._key.hex()}'\"")
        conn.execute("SELECT count(*) FROM sqlite_master")  # force key verification now
        return conn

    @staticmethod
    def _probe_fts5(conn: sqlite3.Connection) -> bool:
        """Return whether FTS5 is compiled in (probe a temp virtual table).

        PyInstaller bundles the build-Python's SQLite, so FTS5 presence is a
        runtime property, not a given. The escaped-LIKE fallback covers absence.
        """
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)"
            )
            conn.execute("DROP TABLE temp._fts5_probe")
            return True
        except _OPERATIONAL_ERRORS:
            return False

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        if self._fts_available:
            # Standard (content-stored) FTS5 table: ``text`` indexed, the
            # pointer/filter columns UNINDEXED. Text is stored (not contentless)
            # so snippet() works. The implicit rowid is the stable per-frame PK;
            # we deliberately do not make ``text`` a key, leaving room for an
            # additive vector sidecar later (R10).
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5("
                "  text,"
                "  recording UNINDEXED,"
                "  timestamp_ms UNINDEXED,"
                "  tokenize = 'unicode61 remove_diacritics 2'"
                ")"
            )
        else:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS content_plain ("
                "  recording TEXT NOT NULL,"
                "  timestamp_ms INTEGER NOT NULL,"
                "  text TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_content_plain_rec "
                "ON content_plain(recording, timestamp_ms)"
            )
        self._create_diary_schema(conn)
        conn.commit()

    def _create_diary_schema(self, conn: sqlite3.Connection) -> None:
        """Create the day-diary block-search table (U6, KTD-8).

        A SECOND table in the SAME store, mirroring ``content_fts``'s shape: the
        block ``text`` (name + bullets) indexed, the pointer/filter columns
        UNINDEXED. Created on EVERY open regardless of ``content_index_enabled``
        (the diary write is default-on — P2/R5), so the first consolidated block
        materialises the hardened store whether or not OCR indexing runs.
        """
        if self._fts_available:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS diary_fts USING fts5("
                "  text,"
                "  recording UNINDEXED,"
                "  block_id UNINDEXED,"
                "  start_ms UNINDEXED,"
                "  end_ms UNINDEXED,"
                "  tokenize = 'unicode61 remove_diacritics 2'"
                ")"
            )
        else:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS diary_plain ("
                "  recording TEXT NOT NULL,"
                "  block_id TEXT NOT NULL,"
                "  start_ms INTEGER NOT NULL,"
                "  end_ms INTEGER NOT NULL,"
                "  text TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_diary_plain_rec "
                "ON diary_plain(recording)"
            )

    @property
    def _table(self) -> str:
        return "content_fts" if self._fts_available else "content_plain"

    @property
    def _diary_table(self) -> str:
        return "diary_fts" if self._fts_available else "diary_plain"

    # -- write -----------------------------------------------------------

    def write_frames(self, recording: str, frames: Iterable[IndexFrame]) -> int:
        """Idempotently persist a chunk's frames for ``recording``.

        Delete-then-insert per ``(recording, timestamp_ms)`` inside a single
        committed transaction, so re-processing the same chunk (``--force`` /
        reconcile) *replaces* rather than duplicates rows, and a corrective
        re-OCR's text supersedes stale text for the same frame. A concurrent
        reader never observes a half-written chunk. Frames with empty text are
        dropped (no point indexing a blank frame). Returns the number of rows
        written.
        """
        if self._conn is None:
            raise sqlite3.OperationalError("content index not open")
        rows = [
            (recording, int(f.timestamp_ms), f.text)
            for f in frames
            if f.text and f.text.strip()
        ]
        conn = self._conn
        table = self._table
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # Idempotency: clear any prior rows for these exact frames first.
            for recording_name, ts_ms, _text in rows:
                cur.execute(
                    f"DELETE FROM {table} WHERE recording = ? AND timestamp_ms = ?",
                    (recording_name, ts_ms),
                )
            cur.executemany(
                f"INSERT INTO {table} (recording, timestamp_ms, text) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._checkpoint()
        return len(rows)

    def write_chunk(
        self,
        recording: str,
        start_ms: int,
        end_ms: int,
        frames: Iterable[IndexFrame],
    ) -> int:
        """Replace all of ``recording``'s rows in ``[start_ms, end_ms)`` with ``frames``.

        Unlike :meth:`write_frames` (per-frame upsert), this clears the WHOLE
        chunk time-range first, so a re-process (``--force`` / reconcile) where a
        frame is now policy- or dedup-skipped drops its stale, less-redacted row
        instead of leaving it behind. One committed transaction → a reader never
        sees a half-written chunk. Returns the number of rows written.
        """
        if self._conn is None:
            raise sqlite3.OperationalError("content index not open")
        rows = [
            (recording, int(f.timestamp_ms), f.text)
            for f in frames
            if f.text and f.text.strip()
        ]
        conn = self._conn
        table = self._table
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                f"DELETE FROM {table} "
                "WHERE recording = ? AND timestamp_ms >= ? AND timestamp_ms < ?",
                (recording, int(start_ms), int(end_ms)),
            )
            if rows:
                cur.executemany(
                    f"INSERT INTO {table} (recording, timestamp_ms, text) "
                    "VALUES (?, ?, ?)",
                    rows,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._checkpoint()
        return len(rows)

    def _checkpoint(self) -> None:
        """Bound ``-wal`` growth after a committed write (the writer owns this)."""
        if self._conn is None:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except _OPERATIONAL_ERRORS:
            pass

    # -- diary write (day-diary block search, U6) ------------------------

    def write_diary_blocks(
        self, recording: str, entries: Iterable[DiaryEntry],
    ) -> int:
        """Replace ALL of ``recording``'s diary rows with ``entries`` (idempotent).

        Delete-then-insert PER RECORDING inside one committed transaction, so a
        re-consolidation pass drops a recording's stale blocks (a block that
        disappeared, or whose text changed) instead of accumulating them — the
        diary equivalent of :meth:`write_chunk`'s whole-range replace. A concurrent
        reader never observes a half-written recording. Entries with empty text
        (no name and no bullets) are dropped. Returns the number of rows written.

        SECURITY (P1): the CALLER must hold :func:`content_index_write_lock` across
        the RE-READ of the source block rows and this write — see
        :func:`write_recording_diary`. This method assumes it, and only performs
        the delete-then-insert.
        """
        if self._conn is None:
            raise sqlite3.OperationalError("content index not open")
        rows = [
            (recording, e.block_id, int(e.start_ms), int(e.end_ms), e.text)
            for e in entries
            if e.block_id and e.text and e.text.strip()
        ]
        conn = self._conn
        table = self._diary_table
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # Whole-recording replace: clear every prior diary row for this
            # recording so a re-consolidation where a block vanished / was renamed
            # / lost bullets drops the stale row instead of leaving it queryable.
            cur.execute(
                f"DELETE FROM {table} WHERE recording = ?", (recording,)
            )
            if rows:
                cur.executemany(
                    f"INSERT INTO {table} "
                    "(recording, block_id, start_ms, end_ms, text) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._checkpoint()
        return len(rows)

    # -- diary search (day-diary block search, U6) -----------------------

    def search_diary(
        self,
        query: str,
        *,
        recording: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> DiarySearchResult:
        """Return ranked block snippets + POINTERS for ``query``. Never raises.

        Same injection-safe binding as :meth:`search` (phrase-escaped FTS5
        ``MATCH`` / ``%``-``_`` escaped LIKE), pointer-only hits (recording,
        block_id, ms span, snippet) — never full bullet text beyond the snippet.
        """
        if not self._available or self._conn is None:
            return DiarySearchResult([], IndexState.STORE_UNAVAILABLE)
        limit = max(1, min(int(limit), _MAX_LIMIT))
        query = (query or "").strip()
        if not query:
            return DiarySearchResult([], IndexState.NO_MATCH)
        try:
            if self._fts_available:
                hits = self._search_diary_fts(query, recording, limit)
                degraded = False
            else:
                hits = self._search_diary_like(query, recording, limit)
                degraded = True
        except _DATABASE_ERRORS as exc:
            logger.warning("diary search failed: %s", type(exc).__name__)
            return DiarySearchResult([], IndexState.STORE_UNAVAILABLE)

        if not hits:
            return DiarySearchResult(
                [], IndexState.INDEX_DEGRADED if degraded else IndexState.NO_MATCH
            )
        return DiarySearchResult(
            hits, IndexState.INDEX_DEGRADED if degraded else IndexState.OK
        )

    def _search_diary_fts(
        self, query: str, recording: str | None, limit: int
    ) -> list[DiaryHit]:
        assert self._conn is not None
        match = _build_fts_match(query)
        if not match:
            return []
        sql = (
            "SELECT recording, block_id, start_ms, end_ms, "
            "snippet(diary_fts, 0, '', '', '…', ?) AS snip, "
            "bm25(diary_fts) AS score "
            "FROM diary_fts WHERE diary_fts MATCH ?"
        )
        params: list[object] = [_SNIPPET_TOKEN_BUDGET, match]
        if recording is not None:
            sql += " AND recording = ?"
            params.append(recording)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(sql, params)
        return [
            DiaryHit(rec, block_id, int(start_ms), int(end_ms), snip or "", float(score))
            for rec, block_id, start_ms, end_ms, snip, score in cur.fetchall()
        ]

    def _search_diary_like(
        self, query: str, recording: str | None, limit: int
    ) -> list[DiaryHit]:
        assert self._conn is not None
        pattern = f"%{escape_like(query)}%"
        sql = (
            "SELECT recording, block_id, start_ms, end_ms, text "
            "FROM diary_plain WHERE text LIKE ? ESCAPE '\\'"
        )
        params: list[object] = [pattern]
        if recording is not None:
            sql += " AND recording = ?"
            params.append(recording)
        sql += " ORDER BY end_ms DESC LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(sql, params)
        return [
            DiaryHit(
                rec, block_id, int(start_ms), int(end_ms),
                like_snippet(text, query), 0.0,
            )
            for rec, block_id, start_ms, end_ms, text in cur.fetchall()
        ]

    # -- search ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        recording: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> SearchResult:
        """Return ranked snippets + pointers for ``query``. Never raises.

        Binds and phrase-escapes the FTS5 ``MATCH`` (never string-formats it)
        and escapes ``%``/``_`` on the LIKE fallback, so a query of ``"``,
        ``*``, ``NEAR`` or ``col:`` is treated as literal text. Returns at most
        ``limit`` hits (clamped to ``[1, _MAX_LIMIT]``), ordered best-first.
        """
        if not self._available or self._conn is None:
            return SearchResult([], IndexState.STORE_UNAVAILABLE)
        limit = max(1, min(int(limit), _MAX_LIMIT))
        query = (query or "").strip()
        if not query:
            return SearchResult([], IndexState.NO_MATCH)
        try:
            if self._fts_available:
                hits = self._search_fts(query, recording, limit)
                degraded = False
            else:
                hits = self._search_like(query, recording, limit)
                degraded = True
        except _DATABASE_ERRORS as exc:
            logger.warning("content search failed: %s", type(exc).__name__)
            return SearchResult([], IndexState.STORE_UNAVAILABLE)

        if not hits:
            return SearchResult(
                [], IndexState.INDEX_DEGRADED if degraded else IndexState.NO_MATCH
            )
        return SearchResult(
            hits, IndexState.INDEX_DEGRADED if degraded else IndexState.OK
        )

    def _search_fts(
        self, query: str, recording: str | None, limit: int
    ) -> list[SearchHit]:
        assert self._conn is not None
        match = _build_fts_match(query)
        if not match:
            return []
        sql = (
            "SELECT recording, timestamp_ms, "
            "snippet(content_fts, 0, '', '', '…', ?) AS snip, "
            "bm25(content_fts) AS score "
            "FROM content_fts WHERE content_fts MATCH ?"
        )
        params: list[object] = [_SNIPPET_TOKEN_BUDGET, match]
        if recording is not None:
            sql += " AND recording = ?"
            params.append(recording)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(sql, params)
        return [
            SearchHit(rec, int(ts), snip or "", float(score))
            for rec, ts, snip, score in cur.fetchall()
        ]

    def _search_like(
        self, query: str, recording: str | None, limit: int
    ) -> list[SearchHit]:
        assert self._conn is not None
        pattern = f"%{escape_like(query)}%"
        sql = (
            "SELECT recording, timestamp_ms, text "
            "FROM content_plain WHERE text LIKE ? ESCAPE '\\'"
        )
        params: list[object] = [pattern]
        if recording is not None:
            sql += " AND recording = ?"
            params.append(recording)
        sql += " ORDER BY timestamp_ms DESC LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(sql, params)
        return [
            SearchHit(rec, int(ts), like_snippet(text, query), 0.0)
            for rec, ts, text in cur.fetchall()
        ]

    # -- delete (privacy-correctness) ------------------------------------

    def delete_recording(self, recording: str) -> int:
        """Purge every row for ``recording`` (full delete / stub-then-delete).

        Deletes from BOTH ``content_fts`` and ``content_plain`` when present, so
        a store that was written under one schema and later opened under the
        other (FTS5 availability differs across builds) can never leave the
        purged recording's sensitive rows behind in the now-inactive table. Also
        cascades into the day-diary block rows (U6): deleting a whole recording /
        day must remove its diary block+bullet text from the global store too
        (R13) — the diary lives in the SAME sidecar but is never uploaded.
        """
        deleted = self._delete(
            "DELETE FROM {table} WHERE recording = ?",
            (recording,),
        )
        deleted += self.delete_recording_diary(recording)
        return deleted

    def delete_recording_interval(
        self, recording: str, start_ms: int, end_ms: int | None
    ) -> int:
        """Purge ``recording`` rows in ``[start_ms, end_ms)`` (retroactive disable).

        ``end_ms=None`` means open-ended (the ``scrub_worker`` trailing interval
        whose unix-seconds upper bound was ``float('inf')``). Deletes from BOTH
        tables when present (see :meth:`delete_recording`).
        """
        if end_ms is None:
            return self._delete(
                "DELETE FROM {table} "
                "WHERE recording = ? AND timestamp_ms >= ?",
                (recording, int(start_ms)),
            )
        return self._delete(
            "DELETE FROM {table} "
            "WHERE recording = ? AND timestamp_ms >= ? AND timestamp_ms < ?",
            (recording, int(start_ms), int(end_ms)),
        )

    def delete_recording_diary(self, recording: str) -> int:
        """Purge every day-diary block row for ``recording`` (U6, R13).

        The whole-recording diary purge — used by the full-recording cascade
        (:meth:`delete_recording`) and available to the U5 retroactive purge. Runs
        over BOTH ``diary_fts`` and ``diary_plain`` when present (FTS5 availability
        differs across builds), so a schema-crossed store never strands rows.
        """
        return self._delete_diary(
            "DELETE FROM {table} WHERE recording = ?",
            (recording,),
        )

    def delete_recording_diary_interval(
        self, recording: str, start_ms: int, end_ms: int | None
    ) -> int:
        """Purge ``recording`` diary blocks OVERLAPPING ``[start_ms, end_ms)`` (U5).

        The retroactive-disable purge the U5 cascade calls under
        ``content_index_write_lock()`` — a block whose ms span intersects a
        disabled interval is removed so its name/bullet text stops being queryable
        (AE5). A block overlaps iff ``start_ms < end_ms`` AND ``end_ms > start_ms``
        (half-open). ``end_ms=None`` is the open-ended trailing interval (the
        ``scrub_worker`` ``float('inf')`` upper bound). Deletes from BOTH diary
        tables when present (see :meth:`delete_recording_diary`).
        """
        if end_ms is None:
            return self._delete_diary(
                "DELETE FROM {table} WHERE recording = ? AND end_ms > ?",
                (recording, int(start_ms)),
            )
        return self._delete_diary(
            "DELETE FROM {table} "
            "WHERE recording = ? AND start_ms < ? AND end_ms > ?",
            (recording, int(end_ms), int(start_ms)),
        )

    def max_indexed_timestamp_ms(self, recording: str) -> int | None:
        """Return the newest indexed ``timestamp_ms`` for ``recording``, or None.

        The screenshot-retention pass (search U5) uses this as the "don't race the
        indexer" high-water mark: a still newer than the last indexed frame is not
        yet safe to evict. Returns None when the store is unavailable or the
        recording has no indexed frames (caller then applies no indexer constraint).
        """
        if not self._available or self._conn is None:
            return None
        newest: int | None = None
        try:
            for table in self._present_tables(self._conn):
                row = self._conn.execute(
                    f"SELECT max(timestamp_ms) FROM {table} WHERE recording = ?",
                    (recording,),
                ).fetchone()
                if row and row[0] is not None:
                    newest = int(row[0]) if newest is None else max(newest, int(row[0]))
        except _DATABASE_ERRORS:
            return None
        return newest

    def _present_tables(self, conn: sqlite3.Connection) -> list[str]:
        """Which of the two content tables actually exist in this store."""
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') "
            "AND name IN ('content_fts', 'content_plain')"
        ).fetchall()
        return [r[0] for r in rows]

    def _present_diary_tables(self, conn: sqlite3.Connection) -> list[str]:
        """Which of the two DIARY tables actually exist in this store (U6)."""
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') "
            "AND name IN ('diary_fts', 'diary_plain')"
        ).fetchall()
        return [r[0] for r in rows]

    def _delete_diary(self, sql_template: str, params: Sequence[object]) -> int:
        """Run ``sql_template`` (a ``{table}`` placeholder) against every present
        DIARY table in one transaction. Returns total rows deleted.

        ``{table}`` is interpolated only from a fixed allowlist of literal table
        names discovered via ``sqlite_master`` — never from caller input.
        """
        if self._conn is None:
            raise sqlite3.OperationalError("content index not open")
        conn = self._conn
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        deleted = 0
        try:
            for table in self._present_diary_tables(conn):
                cur.execute(sql_template.format(table=table), params)
                if cur.rowcount and cur.rowcount > 0:
                    deleted += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._checkpoint()
        return deleted

    def _delete(self, sql_template: str, params: Sequence[object]) -> int:
        """Run ``sql_template`` (a ``{table}`` placeholder) against every present
        content table in one transaction. Returns total rows deleted.

        ``{table}`` is interpolated only from a fixed allowlist of literal table
        names discovered via ``sqlite_master`` — never from caller input — so no
        SQL injection surface is introduced.
        """
        if self._conn is None:
            raise sqlite3.OperationalError("content index not open")
        conn = self._conn
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        deleted = 0
        try:
            for table in self._present_tables(conn):
                cur.execute(sql_template.format(table=table), params)
                if cur.rowcount and cur.rowcount > 0:
                    deleted += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._checkpoint()
        return deleted


# --------------------------------------------------------------------------
# Day-diary write orchestration (the P1 purge-race barrier lives HERE, U6)
# --------------------------------------------------------------------------


def _diary_text(name: str | None, bullets: object) -> str:
    """Join a block's NAME + topic bullets into one searchable string (U6, KTD-8).

    The NARRATIVE is never included. Bullets are the U3 ``metadata.bullets`` list;
    a non-list / missing value contributes nothing. Empty when the block has
    neither a name nor bullets (the caller drops such blocks from the index).
    """
    parts: list[str] = []
    if isinstance(name, str) and name.strip():
        parts.append(name.strip())
    if isinstance(bullets, list):
        parts.extend(str(b).strip() for b in bullets if str(b).strip())
    return "  ".join(parts).strip()


def _read_diary_source_rows(db_path: Path) -> list:
    """Read a recording's task-segment (block) rows — the diary index SOURCE.

    Lazily imports the ledger (``content_index`` stays import-light): the re-read
    of the authoritative block rows is what the P1 barrier keys on, so it must hit
    the live ``recording.db`` at write time, not a caller-cached snapshot.
    """
    from screencap.pipeline_state import PipelineLedger

    return PipelineLedger(Path(db_path)).read_task_segments()


def _diary_entries_from_rows(rows: Iterable) -> list[DiaryEntry]:
    """Project block rows → :class:`DiaryEntry` list (name+bullets, ms span).

    Only rows carrying a ``block_id`` become diary entries (a legacy non-diary
    row has none and cannot be a block). Bullets are parsed from the row's
    ``metadata`` JSON via the shared wire projector so the index and the app read
    the SAME bullets. A block with neither a name nor bullets yields empty text
    and is dropped by :meth:`ContentIndex.write_diary_blocks`.
    """
    from screencap.pipeline_state import _parse_wire_bullets

    entries: list[DiaryEntry] = []
    for row in rows:
        block_id = getattr(row, "block_id", None)
        if not block_id:
            continue
        bullets = _parse_wire_bullets(getattr(row, "metadata", None))
        text = _diary_text(getattr(row, "name", None), bullets)
        if not text:
            continue
        entries.append(
            DiaryEntry(
                block_id=str(block_id),
                start_ms=int(round(float(getattr(row, "start_ts", 0.0)) * 1000)),
                end_ms=int(round(float(getattr(row, "end_ts", 0.0)) * 1000)),
                text=text,
            )
        )
    return entries


def write_recording_diary(
    recording: str,
    db_path: Path | str,
    *,
    store_path: Path | str | None = None,
    rows_reader=None,
) -> int:
    """Replace ``recording``'s diary rows from its LIVE block rows (U6, P1-safe).

    The ONE diary write entry point (the terminal-stage consolidation hook calls
    it live and at finalize). Independent of ``content_index_enabled`` — the diary
    write is default-on (P2/R5), so this ALWAYS runs after consolidation and
    materialises the hardened store on the first named block.

    SECURITY (P1 — the purge-race barrier): the whole re-read + write is held
    under ``content_index_write_lock(require_cross_process=True)``, and the block
    rows are RE-READ from ``recording.db`` UNDER that lock immediately before the
    write — the diary equivalent of ``index_core``'s unlink-before-write re-``stat``
    barrier. This closes the resurrection window: a consolidation pass that read
    block rows BEFORE a retroactive-disable purge deleted them must not re-insert
    the disabled app's text AFTER the purge's diary delete. Under the lock the two
    orderings converge to "purged": either this write lands first and the purge
    (U5, same lock) then deletes it, or the purge runs first (deleting the block
    rows from ``recording.db`` and the diary rows) and this re-read then sees the
    purged rows and writes clean text.

    ``require_cross_process=True`` is load-bearing: the diary write runs in the
    DAEMON process (terminal-stage segmentation), a DIFFERENT process from the
    recorder subprocess that runs the retroactive-disable purge, so the in-process
    ``threading.Lock`` does NOT serialize the two — only the flock does (same
    posture as ``index_core`` / ``corpus_migrate``, both cross-process daemon
    writers). On a filesystem without flock support we DECLINE the write (return
    ``-1``) rather than write unserialized, which could resurrect just-purged
    text; the purge still deletes, so its result stands. On the default local
    APFS store flock works and the write proceeds normally.

    Empty-store guard: when there is nothing to write AND the store file does not
    exist yet, no (empty) store is created — mirrors the content pass. When the
    store already exists the delete-then-insert still runs so a re-consolidation
    that dropped every block clears the stale rows.

    ``rows_reader`` is an injectable seam (defaults to :func:`_read_diary_source_rows`)
    for the contention tests. Returns the number of rows written, or ``-1`` when
    the cross-process lock was unavailable and the write was declined.
    """
    if store_path is None:
        store_path = default_index_path()
    store_path = Path(store_path)
    reader = rows_reader or _read_diary_source_rows
    try:
        with content_index_write_lock(require_cross_process=True):
            # RE-READ the authoritative block rows UNDER the lock (the barrier).
            rows = reader(db_path)
            entries = _diary_entries_from_rows(rows)
            if not entries and not store_path.exists():
                return 0  # nothing to index and no store to clear — don't create one.
            with ContentIndex(store_path) as store:
                if not store.available:
                    return 0
                return store.write_diary_blocks(recording, entries)
    except CrossProcessLockUnavailable:
        # No cross-process flock on this filesystem: decline rather than write a
        # diary row the recorder-subprocess purge cannot serialize against.
        return -1


def like_snippet(
    text: str, query: str, *, width: int = 80, collapse_newlines: bool = False,
) -> str:
    """Build a bounded excerpt around the first case-insensitive match.

    The FTS5 path uses SQLite's ``snippet()``; the degraded LIKE path and the
    daemon's transcript scan have no equivalent, so produce a comparable window
    here. Shared by both (``collapse_newlines`` flattens multi-line transcript
    text into a single snippet line).
    """
    lowered = text.lower()
    idx = lowered.find(query.lower())
    if idx < 0:
        excerpt = text[:width].strip()
        return excerpt.replace("\n", " ") if collapse_newlines else excerpt
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(query) + width // 2)
    excerpt = text[start:end].strip()
    if collapse_newlines:
        excerpt = excerpt.replace("\n", " ")
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"
