# Architecture

Living architecture docs for ScreenCap. Each file describes how a subsystem works **today** and the load-bearing invariants you cannot break without thinking carefully.

## How to use these docs

| Doc type | Lifecycle | Answer |
|---|---|---|
| `architecture/` (this dir) | Living — updated when code changes | "How does X work today?" |
| `decisions/` | Immutable | "Why did we choose X on date Z?" |
| `research/` | Snapshots | "What did we learn during this investigation?" |
| `solutions/` | Immutable | "How did we solve this specific problem?" |
| `plans/`, `tickets/`, `todos/` | Living — closes when work ships | "What are we working on?" |

Rule of thumb: if a fact is stable and someone joining tomorrow needs it, it lives here. Point-in-time investigations stay in `research/`. Why-we-chose-this lives in `decisions/`.

Do not put file:line references in these docs — they rot fastest. Refer to modules and concepts.

## Project at a glance

ScreenCap is a macOS CLI for screen recording with a privacy-aware capture pipeline, post-recording redaction, and optional cloud upload + LLM-based task segmentation.

```
                    ┌─────────────────────────────────┐
                    │          screencap CLI          │
                    └────────────────┬────────────────┘
                                     │
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
   SessionController (persistent)              Legacy one-shot (env-gated)
              │
              ▼
   ┌─────────────────────────────────────────┐
   │   engine.Recorder (multi-process)       │
   │   reader threads → event_q              │
   │   → event_processor                     │
   │   → write queues → writer processes     │
   │   → recording.db / chunk_*.mp4 /        │
   │     audio_*.flac / events_*.jsonl       │
   │                                         │
   │   RecorderPrivacyFilter gates           │
   │   screenshots / video / keystrokes      │
   └─────────────────────────────────────────┘
              │
              ▼
   Per-chunk: scrub_pipeline → events_NNNN.jsonl + manifest
              │
              ▼  (on upload)
   scrubber.scrub_recording → <name>-scrubbed/
              │
              ▼
   Cloud Function (signed URLs) → GCS
              │
              ▼
   Cloud Run process-recording → Gemini segmentation → timeline.json
```

## Subsystem index

| Doc | What it covers |
|---|---|
| [recording-engine.md](./recording-engine.md) | Multi-process capture: reader threads, event_q, writer processes, signal handling, chunk rotation, on-disk layout |
| [event-system.md](./event-system.md) | Pydantic event models, the 11-stage processing pipeline, raw → semantic event promotion |
| [database.md](./database.md) | The `recording.db` SQLite schema, SQLAlchemy + raw access patterns, in-place migration |
| [privacy.md](./privacy.md) | Two-layer enforcement: capture-time filter (action matrix, blocking sources, fail-closed) + detection pipeline (PII/secrets) |
| [scrubbing.md](./scrubbing.md) | Three scrub flavors: post-hoc full scrub, per-chunk during cloud-intent recording, live cascade-delete from menubar |
| [export-pipeline.md](./export-pipeline.md) | events.jsonl format v2, the shared processing chain, privacy-aware window switches |
| [segmentation.md](./segmentation.md) | Task segmentation: client v1 idle-gap vs Cloud Run v2 LLM (Gemini), validation + fallback |
| [session.md](./session.md) | SessionController lifecycle, menubar subprocess (legacy + session modes), IPC topology, post-process worker |

## Cross-cutting conventions

These are patterns the codebase mostly follows. Some are enforced by structure (layering, recording dir layout); others are aspirational and have exceptions. When in doubt, check what the surrounding code does.

- **Layering**: `engine/` is the core capture machine (heavy deps, headless-unfriendly). `screencap/` is the user-facing layer. `scripts/process-recording/` and `scripts/cloud-function/` are separate deployables. *Enforced.*
- **Deferred imports**: Heavy imports happen inside Click command bodies, not at module top, to keep `screencap --help` fast. *Enforced for the CLI hot path; not universal.*
- **Atomic writes**: Critical state files (config.toml via `save_config_atomic`, JSONL exports, scrubbed outputs) write through tempfile + `os.rename`. *Aspirational, not universal — e.g. `task_manifest` writes manifests directly with `Path.write_text()`. Add atomic writes when the file matters; not every write needs it.*
- **Config priority**: env var > `~/.screencap/config.toml` > hardcoded default. After every write to the TOML, call `invalidate_config_cache()`. *Enforced for `screencap/config.py` getters.*
- **Path traversal guard**: User-supplied recording names go through `resolve_recording_dir()`. *Enforced for CLI-facing entry points (export, scrub, view, info, etc.).*
- **No bare `print()` for user output**: User-facing output goes through `rich.console.Console`. Module-level `console = Console()` is the pattern. *Enforced for the CLI; library modules may use `loguru` instead.*
- **`from __future__ import annotations`**: Common, but **not universal** — some modules (notably most of `privacy/`, `menubar.py`, `app_discovery.py`) omit it. Add it for new files unless there's a specific reason not to.
- **Recording dirs** live at `~/.screencap/recordings/<name>/`. The directory IS the unit of identity; `.recording_id` carries the canonical name. *Enforced.*

## When to add a new file here

Add a new architecture doc when a subsystem grows enough that its mental model no longer fits in another doc. Otherwise, extend an existing one. If you're tempted to add a file because something changed, you probably want to *update* an existing doc, not write a new one.

When you change a subsystem in code, touch its architecture doc in the same PR. If the change doesn't affect the architecture doc, that's fine — say so in the PR description. The discipline is checking, not always editing.

## Relationship to CLAUDE.md files

CLAUDE.md = invariants and gotchas for an agent ("don't do X", "always do Y"). Architecture docs = how it works for a human reader. They're complementary; CLAUDE.md is short and rule-shaped, architecture docs are descriptive and diagram-heavy.

If you find yourself writing the same rule in both: put the descriptive context in the architecture doc and a one-liner reference in CLAUDE.md.
