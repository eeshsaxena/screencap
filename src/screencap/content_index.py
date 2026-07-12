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
        conn.commit()

    @property
    def _table(self) -> str:
        return "content_fts" if self._fts_available else "content_plain"

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
        purged recording's sensitive rows behind in the now-inactive table.
        """
        return self._delete(
            "DELETE FROM {table} WHERE recording = ?",
            (recording,),
        )

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
