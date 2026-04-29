# Database

## What it does

Stores all per-recording state: events, screenshots, window state, audio info, performance counters. Each recording owns its own SQLite file inside its directory.

## One schema, two access layers

There is now a single on-disk schema — `recording.db` — managed by SQLAlchemy in the engine and read directly via raw `sqlite3` from the screencap layer. The legacy `capture.db` schema (upstream-project leftover, two tables with JSON blobs) was removed; recordings produced in that format are no longer supported.

```
~/.screencap/recordings/<name>/
   │
   └── recording.db    ← SQLAlchemy-managed, 8 typed tables
       created by: engine writer processes (live recording)
       owner: src/screencap/engine/db/
```

`catalog.find_db()` resolves to `recording.db` if it exists, otherwise `None`. The 11-stage event processing pipeline runs against this typed schema.

## `recording.db` schema (SQLAlchemy)

8 tables. All inherit from a `Base` with a standard naming convention. Numeric columns use a `ForceFloat` `TypeDecorator` to coerce to Python `float` on read.

| Table | Purpose | Key columns |
|---|---|---|
| `recording` | One row per recording | `id`, `timestamp` (identity key), `monitor_width/height`, `pixel_ratio`, `task_description`, `video_start_time`, `config` (JSON), `original_recording_id` |
| `action_event` | Mouse/keyboard/gesture events | `name` (legacy string discriminator), `timestamp`, `recording_id`, `screenshot_timestamp`, `window_event_timestamp`, mouse fields, key fields (raw + canonical), `parent_id`, `element_state` (JSON), `disabled` |
| `window_event` | Window state snapshots | `timestamp`, `state` (JSON AX tree), `title`, bounds, `window_id`, `app_bundle_id`, `app_version`, `browser_url`, `app_name` |
| `screenshot` | Frame BLOBs | `timestamp`, `png_data`, `png_diff_data`, `png_diff_mask_data`, `image_path` |
| `audio_info` | Per-segment audio metadata | `timestamp`, `sample_rate`, `words_with_timestamps` (JSON-string) |
| `performance_stat` | Per-event timing | `event_type`, `start_time`/`end_time` (ns), `window_id` |
| `window_geometry` | All-windows snapshot per frame | `screenshot_timestamp` (indexed), `window_list_json` |
| `memory_stat` | RSS over time | `memory_usage_bytes`, `timestamp` |
| `network_event` | HTTP/HTTPS + WebSocket capture metadata (V1 only when `screencap start --network`) | `kind` enum (`request`/`response`/`ws_upgrade`/`ws_frame`/`drop_burst`), `flow_id`, `method`, `url`, `host`, `status`, `headers_json`, `body_size`, `body_sha256` (raw 32 bytes), `content_type`, `direction`, `frame_type`, `details_json` (kind-dependent payload), `timestamp_ns` (indexed for sort) |

Relationships cascade `all, delete-orphan`. `Recording.original_recording_id` self-references for copies.

`window_geometry.screenshot_timestamp` is the only explicitly-indexed column outside primary keys — used by the scrubber's selective-mask path.

## Connection pragmas

`engine/db/__init__.py` registers a `connect` event listener on every SQLAlchemy engine. Every new connection runs:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -64000;   -- 64 MB
PRAGMA busy_timeout = 5000;   -- 5s
```

The chunk processor and scrub worker, which use raw `sqlite3` from the screencap layer, set their own pragmas (`busy_timeout=5000`, often `query_only=ON`).

## Writes

`engine/db/crud.py` uses module-level buffers (`action_events`, `screenshots`, `window_events`, etc.) and SQLAlchemy Core `session.execute(sa.insert(table), buffer)` for throughput, NOT the ORM. `BATCH_SIZE` defaults to 1 but the recorder overrides to 50.

`insert_recording()` is the only ORM-style write — it needs the autoincrement PK back. Everything else is buffered Core inserts. `flush_buffers(session)` is called on shutdown.

## Reads

Reader patterns in use:

| Reader | Layer | DB access |
|---|---|---|
| `engine.CaptureSession` | engine | SQLAlchemy ORM via `get_session_for_path()` |
| `chunk_processor._export_events` | screencap | Raw `sqlite3` (read-only via `query_only=ON`) |
| `scrubber._scrub_db` | screencap | Raw `sqlite3` with separate read+write cursors |
| `scrub_worker` | privacy | Raw `sqlite3` with `BEGIN IMMEDIATE` transactions |
| `catalog.list_recordings` | screencap | Raw `sqlite3` |

## Migration

`_migrate_schema(db_path)` in `engine/db/__init__.py` runs on `get_session_for_path()`. It compares `Base.metadata` columns against `PRAGMA table_info(<table>)` and issues `ALTER TABLE <table> ADD COLUMN <name> <type>` for missing columns. Dropping or renaming columns is NOT supported by this migration, and it never `CREATE TABLE`s — so older recordings predating a new table (e.g. `window_geometry`) never gain it on read. Consumers must guard table presence at the call site.

## Load-bearing invariants

- **`find_db()` returns `recording.db` or `None`.** The legacy `capture.db` path is gone.
- **WAL mode + `synchronous=NORMAL`.** Required for concurrent reads while writers commit. Don't change to FULL — it'll trigger fsync per commit and trash recording performance.
- **Writes from the screencap layer use raw sqlite3, not SQLAlchemy.** Don't import SQLAlchemy in `chunk_processor.py` or `scrub_worker.py` — adds startup time and creates schema-binding coupling.
- **Migration adds columns only.** `_migrate_schema` is one-directional. If you remove a column from the model, old DBs will still have it as a dead column. Plan accordingly.
- **`disabled` column on `action_event` is the soft-delete flag.** `CaptureSession.raw_events()` filters by `disabled=False`. Don't actually DELETE rows for soft-removal — flip the flag.
- **`window_geometry.screenshot_timestamp` is indexed.** The scrubber uses it for selective masking. Removing the index will make scrubbing very slow.
- **Recursive CTE for action_event subtree delete.** `action_event.parent_id` self-references. The scrub worker uses `WITH RECURSIVE descendants(id) AS ... DELETE FROM action_event WHERE id IN descendants` to drop entire trees, not just top-level rows.
- **WAL checkpoint after live deletes.** The scrub worker runs `PRAGMA wal_checkpoint(RESTART)` after committing — without it, deleted rows are still in the WAL and visible to concurrent readers.

## Before you change it

- Adding a column to a table: add to `engine/db/models.py`. The next read via `get_session_for_path()` will run `_migrate_schema()` and `ALTER TABLE`. No manual migration script needed.
- Adding a new table: define the model, then either bump a schema version or accept that older DBs won't have it (use `IF EXISTS` checks for backward read compatibility, e.g. `catalog._read_recording_meta`).
- Changing index strategy: `Base.metadata.create_all` only creates indices on first DB creation. Existing DBs need manual `CREATE INDEX IF NOT EXISTS` in `_migrate_schema`.
- Removing or renaming a column: don't. The migration is one-way. If you must, plan a full read-write-rewrite migration outside `_migrate_schema`.

## See also

- [recording-engine.md](./recording-engine.md) — what the writers write
- [event-system.md](./event-system.md) — `dict_to_action_event` converts rows to events
- [scrubbing.md](./scrubbing.md) — how the scrubber and live worker mutate the DB
- [export-pipeline.md](./export-pipeline.md) — chunk processor's raw-sqlite3 reads
- [network-capture.md](./network-capture.md) — `network_event` table written by the V1 network capture pipeline; metadata-only in V1, body columns added in V1.5 alongside `network_event_meta` for KEK/DEK
