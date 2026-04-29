# Scrubbing

## What it does

Post-capture redaction of PII, secrets, and policy-blocked content from a completed recording's database, JSONL exports, screenshots, and transcripts.

The naming is misleading: "scrubber" sounds like a privacy module, but it lives at the screencap layer because it's a *workflow over a recording directory* — copying files, mutating DBs, scrubbing transcripts, writing audit logs. It uses the privacy module as its primitive library.

## Three flavors, one shared engine

```
┌─────────────────────────────────────────────────────────────┐
│  privacy/    (rules + primitives + LIVE during-recording)   │
│                                                             │
│  DetectionPipeline · classifier · evaluator · masking       │
│  KEYSTROKE_CONTENT_FIELDS · ReasonCode · AuditEntry         │
│  scrub_worker.py (live cascade-delete)                      │
└────────────┬────────────────────────────────────────────────┘
             │ imported by
             ▼
┌─────────────────────────────────────────────────────────────┐
│  scrub_pipeline.py    (SHARED post-capture engine)          │
│                                                             │
│  scrub_text() · scrub_events_jsonl() · mask_screenshots()   │
│  build_blocked_intervals() · build_scrub_context()          │
│  scrub_transcripts() · scrub_manifest()                     │
└──┬────────────────────────────────────────────┬─────────────┘
   │ called by                                  │ called by
   ▼                                            ▼
scrubber.py                          chunk_processor.py
(post-hoc full-recording             (per-chunk during cloud-intent
scrub_recording → <name>-scrubbed/)   recording, before upload)
   │                                            │
   ▼                                            ▼
CLI: screencap scrub <name>          recorder.py (auto)
CLI: screencap upload (auto)         SessionController
```

### Flavor 1 — Post-hoc full scrub (`scrubber.scrub_recording`)

Triggered by `screencap scrub <name>` or automatically before `screencap upload`. Never mutates the source. 18 steps:

1. Validate name → `resolve_recording_dir` (path-traversal guard).
2. Build `app_allowlist` from `system_metrics.json` running apps → fed as `person_allowlist` to PiiDetector.
3. Construct `pipeline = create_default_pipeline(pii_engine=..., person_allowlist=..., require_pii=True)`.
4–7. `rmtree` existing `<name>-scrubbed/`, then `shutil.copytree` skipping media (`.mp4/.flac/.wav/.m4a/.aac/.ogg/.opus`), `.upload_status.json`, `viewer.html`.
8–9. Load privacy config; force `PUBLIC` mode if `.recording_intent` destination is `cloud` or `both`.
10. `build_scrub_context(db_path, evaluator, classifier)`: builds blocked intervals (app policy + secure field), loads window events.
11. `mask_screenshots(dst/screenshots, ctx)`: per-screenshot policy decision → EXCLUDE / MASK_WINDOW / MASK_REGION / OCR_FALLBACK / ALLOW + background masking.
12. `_null_db_rows_for_intervals`: UPDATE `action_event` keystroke fields and `window_event` title/state/browser_url to NULL for every blocked time range.
13. `_scrub_db`: deletes `screenshot` BLOB table + `audio_info` table; scrubs text/JSON columns via the detection pipeline.
14. `build_xref_lookup(raw_detections)` → `ctx.xref_detections` for cross-referencing element_state PII against keystrokes.
15. `_scrub_events_jsonl`: globs `events*.jsonl`, dispatches each to `scrub_pipeline.scrub_events_jsonl`.
16. `scrub_transcripts`: globs `transcript*.json/txt`.
17. `_scrub_metrics`: rule-based `<REDACTED>` for `hostname`, `wifi.ssid`, `wifi.bssid`.
18. `_write_audit_log` → `privacy_audit.json`.

### Flavor 2 — Per-chunk scrub during recording (chunk processor)

The chunk processor calls `scrub_pipeline` functions inline at each chunk rotation: `scrub_events_jsonl` over `events_NNNN.jsonl`, `mask_screenshots` over `screenshots/` for that time range, `scrub_transcripts` for `transcript_NNNN.*`. Output is uploaded directly.

**Gated by `_scrub_enabled`.** The scrub call (`_scrub_chunk_files`) only runs when `self._scrub_enabled and self._pipeline is not None`. `--no-scrub` on `screencap start` sets `scrub_enabled=False` and skips this entirely.

There are two upload paths, and they treat scrubbing differently:

- **Live chunk upload** (during recording, when `upload_enabled=True` on the chunk processor): uploads each chunk as it rotates. Scrubbing is gated by `_scrub_enabled`. With `--no-scrub`, chunks reach GCS unscrubbed.
- **Manual `screencap upload`** (after recording, separate command): **always** runs `scrub_recording()` first, regardless of `--no-scrub` at recording time. The CLI literally has the comment "Always scrub before upload" and aborts upload on scrub failure. The scrubbed `<name>-scrubbed/` copy is what gets uploaded.

What this means in practice:

| State | Capture-time enforcement | Per-chunk scrub (live upload) | Pre-upload scrub (manual `screencap upload`) |
|---|---|---|---|
| Cloud intent, scrub on, live upload | ✓ | ✓ | n/a (chunks already uploaded) |
| Cloud intent, `--no-scrub`, live upload | ✓ | ✗ | n/a (chunks already uploaded) |
| Manual `screencap upload` | ✓ (during recording) | n/a (not live) | ✓ always |
| Local intent, no upload | ✓ (per local mode) | n/a | only if user runs `screencap scrub` |

Capture-time enforcement always applies (the `RecorderPrivacyFilter` runs regardless of `--no-scrub`). What `--no-scrub` disables is the post-capture detection-pipeline pass over text content during live chunk upload. EXCLUDE-blocked apps are still never written to disk; what may slip through to GCS during a `--no-scrub` live upload is PII/secrets in events from non-blocked apps.

### Flavor 3 — Live cascade-delete (`privacy/scrub_worker.py`)

A `threading.Thread` in the recorder process that listens on `disable_q`. When the user clicks "Disable" on an app/domain in the menubar, the worker:

1. Triggers `wait_for_writer_flush` (shared `flush_lock` with chunk processor) so the latest writer batches commit.
2. `_compute_target_set_and_intervals`: finds all window event timestamps for the target bundle/domain, plus active intervals (with 2s pre-roll for URL-detection lag).
3. Pre-queries screenshot rows in those intervals.
4. `BEGIN IMMEDIATE` transaction:
   - Recursive CTE delete on `action_event.parent_id` to drop entire subtrees.
   - Delete `window_geometry` by `screenshot_timestamp`, `screenshot` by id, `window_event` by timestamp.
   - Clean orphan `action_event` rows whose `window_event_timestamp` no longer exists.
5. Commit, `PRAGMA wal_checkpoint(RESTART)`.
6. Delete `.jpg` files on disk (path-resolved against capture dir).
7. `DisableLogWriter` appends to `.menubar_disable_log.jsonl`.

In parallel, `privacy/persistence.py:persist_disable` writes the user's "always disable" decision to `~/.screencap/config.toml` (under `fcntl.LOCK_EX` for cross-process safety). That doesn't affect the current recording — the IPC override already reached `RecorderPrivacyFilter`.

## The shared engine — `scrub_pipeline.py`

Everything that's not orchestration. Public functions:

- `scrub_text(text, pipeline, anonymizer, result)` — atomic unit. Detect + anonymize. Returns `<SCRUB_FAILED>` on any exception (fail-closed).
- `build_blocked_intervals(window_events, evaluator, classifier)` — emits one `BlockedInterval` per span where `decision.action in BLOCK_ACTIONS`.
- `build_secure_field_intervals(action_events, hold_seconds)` — scans `element_state` for `AXSecureTextField`.
- `merge_intervals(...)` — concat + sort + collapse adjacent.
- `find_blocked_interval(intervals, ts)` — bisect lookup.
- `build_scrub_context(db_path, evaluator, classifier, ...)` — factory bundling all per-op state.
- `scrub_events_jsonl(jsonl_path, pipeline, anonymizer, ctx, db_dir=...)` — line-by-line: skip `_meta`, null content for blocked intervals, target detection on `key.type`, cross-reference detection, comprehensive recursive JSON scrub. Atomic `os.rename`.
- `mask_screenshots(screenshot_dir, ctx, ...)` — policy-aware masking with z-order-respecting bitmap composition + Phase 2 OCR with dHash cache for ALLOW frames.
- `scrub_transcripts(transcript_path, pipeline, anonymizer, result)` — JSON or plain text; scrubs `text`, `segments[].text`, `words[].word`.
- `scrub_manifest(manifest_path, ...)` — v1 `dominant_title` only; v2 manifests skipped.

## Output: `privacy_audit.json`

Written by `_write_audit_log`. List of `AuditEntry` dicts:

```json
{
  "timestamp": 1234567.0,
  "surface": "screenshot" | "event" | "keystroke" | "db_field",
  "action": "exclude" | "mask_window" | "text_redact" | ...,
  "reason": "policy_excluded_app" | "context_email_surface" | ...,
  "context_class": "email",
  "evidence_type": "bundle_id" | "domain" | "title"
}
```

Never contains raw text, OCR words, full titles, domains, or query parameters — only categorical evidence.

## Load-bearing invariants

- **`scrub_recording` never mutates the source.** Always copies first to `<name>-scrubbed/`. Source remains untouched even on failure.
- **Cloud destination forces PUBLIC mode.** If `.recording_intent.destination` is `cloud` or `both`, scrubber overrides config-file mode. Local recordings respect config.
- **Upload aborts on scrub failure.** `screencap upload` calls `scrubber.scrub_recording` first; any exception aborts the upload for that recording.
- **`scrub_text` returns `<SCRUB_FAILED>` on error.** Fail-closed for text. Callers that see this sentinel should not assume the original text is preserved.
- **JSONL writes are atomic.** Always `.tmp` + `os.rename`. On any exception, the `.tmp` is deleted; the original survives intact.
- **`KEYSTROKE_CONTENT_FIELDS` is shared with privacy enforcement.** When nulling on blocked intervals, both the capture-time and post-capture paths use the same frozenset.
- **`mask_screenshots` z-order matters.** When `respect_z_order=True`, the bitmap is built back-to-front so foreground non-sensitive windows cut out background masked regions. Removing this would mask too much.
- **`scrub_worker` uses `BEGIN IMMEDIATE`.** Required to lock the DB against engine writers during cascade delete. Without it, race conditions can leave orphans.
- **WAL checkpoint after live deletes.** Without `wal_checkpoint(RESTART)`, deleted rows remain in WAL and are visible to concurrent readers.
- **Recursive CTE on `action_event.parent_id`.** Required to delete entire subtrees (drag children, click children). A flat DELETE leaves orphans.
- **`DisableLogWriter` first call writes a `_meta` header.** Schema versioning for `.menubar_disable_log.jsonl`.
- **Secure-field intervals use a hold timer.** 1.0s default; allows expiry. Don't make it `inf`.
- **`scrub_manifest` skips v2.** v2 manifests have no `dominant_title` to scrub. Skipping is correct, not a bug.

## Before you change it

- Adding a new field that holds user content: scrub it. Check if it goes in `KEYSTROKE_CONTENT_FIELDS` (capture-time + post-capture) or needs its own scrub path (e.g., a new transcript-like file → extend `scrub_transcripts`).
- Adding a new file type to a recording: extend `_copytree_ignore` if it shouldn't be copied to `-scrubbed/`, or add a scrub step if it should.
- Changing the column list scrubbed in `_scrub_recording_schema`: the function uses `_try_scrub_text_column` / `_try_scrub_json_column` which silently skip missing columns — safe for older DBs.
- Adding a new `PrivacyAction`: decide its membership in `BLOCK_ACTIONS`, `KEYSTROKE_NULL_ACTIONS`, `VIDEO_BLOCK_ACTIONS`. The post-capture scrubber and capture-time enforcement both branch on these sets.
- Changing the disable cascade: the recursive CTE depends on `parent_id` self-reference. Re-test with multi-level drag children.

## See also

- [privacy.md](./privacy.md) — the rules + detection pipeline this consumes
- [database.md](./database.md) — schema being mutated
- [export-pipeline.md](./export-pipeline.md) — chunk-time scrub coordination
- [recording-engine.md](./recording-engine.md) — `disable_q` IPC and writer flush
- `decisions/` — why three flavors, why scrubber lives outside privacy/
