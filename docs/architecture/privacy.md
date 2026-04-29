# Privacy

## What it does

Two-layer enforcement: capture-time gates that block sensitive content from being recorded at all, plus a post-capture detection pipeline that finds PII and secrets in any text that did get recorded.

The two layers share data structures (`PrivacyAction`, `PrivacyMode`, `ContextClass`, `AuditEntry`, `KEYSTROKE_CONTENT_FIELDS`) but operate at completely different times.

## Layer 1: capture-time enforcement

Lives in `privacy/recorder_enforcement.py`. Observed by the engine's `event_processor` thread. Queried before every screenshot, video frame, and action event write.

```
window event arrives
        │
        ▼
DefaultContextClassifier.classify()      ← bundle ID / domain / title heuristics
        │
        ▼  (FrameMetadata, ContextClass)
DefaultPolicyEvaluator.evaluate()        ← 5-level precedence + matrix
        │
        ▼  (ActionDecision: PrivacyAction + ReasonCode)
RecorderPrivacyFilter.on_window_event()  ← updates _blocked_reasons dict
        │
        ▼
get_capture_disposition() → CaptureDisposition(screen_allowed, video_allowed, keystrokes_allowed)
```

### `PrivacyAction` (severity-ordered)

| Action | Effect |
|---|---|
| EXCLUDE | drop screenshot/video/keystroke entirely |
| MASK_WINDOW | screenshot captured + masked at scrub time; video dropped; keystrokes nulled |
| MASK_REGION | sub-window masking (deferred — not implemented) |
| TEXT_REDACT | content kept; redacted via NER pipeline at scrub time |
| OCR_FALLBACK | content kept; OCR extracts text for verification |
| ALLOW | normal capture |

`stricter(a, b)` returns the more restrictive action. `BLOCK_ACTIONS = {EXCLUDE}`. `KEYSTROKE_NULL_ACTIONS = VIDEO_BLOCK_ACTIONS = {EXCLUDE, MASK_WINDOW}`.

### `ContextClass × PrivacyMode` matrix

13 context classes × 3 modes. Validated at module import (raises `RuntimeError` if any cell is missing).

| ContextClass | PUBLIC | SHARED | INTERNAL |
|---|---|---|---|
| PASSWORD_MANAGER | EXCLUDE | EXCLUDE | EXCLUDE |
| BANKING | EXCLUDE | EXCLUDE | MASK_WINDOW |
| EMAIL | MASK_WINDOW | MASK_REGION | TEXT_REDACT |
| CHAT | MASK_WINDOW | MASK_REGION | TEXT_REDACT |
| CALENDAR | MASK_WINDOW | MASK_REGION | TEXT_REDACT |
| VIDEO_CALL | MASK_WINDOW | MASK_REGION | TEXT_REDACT |
| BROWSER_UNVERIFIED | MASK_WINDOW | OCR_FALLBACK | ALLOW |
| CODE_EDITOR_TERMINAL | TEXT_REDACT | TEXT_REDACT | ALLOW |
| ADMIN_CONSOLE | TEXT_REDACT | TEXT_REDACT | ALLOW |
| AUTH_FLOW | EXCLUDE | EXCLUDE | MASK_WINDOW |
| PAYMENT_FLOW | EXCLUDE | EXCLUDE | MASK_WINDOW |
| CLOUD_STORAGE | MASK_WINDOW | MASK_REGION | ALLOW |
| UNKNOWN | ALLOW | ALLOW | ALLOW |

### 5-level precedence (`DefaultPolicyEvaluator.evaluate()`)

1. **`exclude_apps`** — bundle ID in user's exclude list → `EXCLUDE`. Cannot be overridden.
2. **`allow_apps`** — bundle ID in user's allow list → matrix-EXCLUDE still wins (password manager allowlisted is still EXCLUDE); `BROWSER_UNVERIFIED` falls through for domain refinement.
3. **`mask_domains`** — `stricter(MASK_WINDOW, matrix_action)`.
4. **`mask_title_patterns`** — `stricter(MASK_WINDOW, matrix_action)` if any compiled regex matches.
5. **Matrix default** — `_ACTION_MATRIX[(context_class, mode)]`.

### Blocking sources

`RecorderPrivacyFilter._blocked_reasons: dict[str, float]` maps reason → expiry (`inf` for indefinite). Three independent gates compute their own intersections of reason keys:

- `screen_block_reasons = {"app_policy", "secure_field", "filter_error", "initial"}`
- `keystroke_block_reasons = screen_block_reasons | {"app_keystrokes", "secure_input"}`
- `video_block_reasons = screen_block_reasons | {"app_video"}`

| Reason | Gates | Set when |
|---|---|---|
| `initial` | all | construction (fail-closed start) |
| `filter_error` | all | exception in event handler |
| `app_policy` | all | EXCLUDE-blocked app frontmost |
| `app_keystrokes` | keystroke | MASK_WINDOW app frontmost |
| `app_video` | video | MASK_WINDOW app frontmost |
| `secure_input` | keystroke only | `CGSIsSecureEventInputSet` returned True |
| `secure_field` | all | `AXRole/AXSubrole == "AXSecureTextField"` |

When an app transitions blocked→allowed, the reason isn't deleted — it's set to `now + 1.0s` (`DEFAULT_TRANSITION_HOLD_SECONDS`). The 1.0s hold covers macOS Cmd+Tab animation (200–350ms with margin).

### Context classification

`DefaultContextClassifier.classify()` 5-step priority:

1. User config override (`app_classes` dict) — except `BROWSER_UNVERIFIED` falls through.
2. `BUNDLE_ID_MAP` — ~90 known bundle IDs (password managers, banking, email, chat, calendar, video call, code editors, admin consoles).
3. Known browser (`BROWSER_BUNDLE_IDS`): if domain present, run `_classify_domain()` (O(1) hash lookup with parent-domain walk-up, skipping TLD-like suffixes like `co.uk`) and `_detect_keyword_flow()` (URL path + subdomain tokens for auth/payment); pick stricter.
4. Title heuristics (regex match against compiled patterns: inbox, slack, calendar, meeting, password, 2fa, bank).
5. Explicit `UNKNOWN`.

### Domain index

Built from two sources merged at startup:

- **UT1 blocklists** (`src/screencap/privacy/data/ut1/`) — 6 categories (bank, financial, webmail, social_networks, chat, vpn) downloaded from `olbat/ut1-blacklists` mirror. First category to claim a domain wins.
- **Curated supplement** (`privacy/data/supplement.py`) — ~60 entries for password manager URLs, cloud admin consoles, video call, banking subdomains. Overrides UT1 on conflict.

Parent-domain matching at query time strips one label at a time, skipping TLD-like parents via `_SLD_SUFFIXES`.

## Layer 2: detection pipeline

Lives in `privacy/__init__.py` + `pii.py`, `regex.py`, `secrets.py`, `resolver.py`, `filters.py`, `entity_mapping.py`, `ocr.py`. Operates on text strings, returns `Detection` spans, used at scrub time.

```
text in
   │
   ▼
normalize_text()                    NFKC + strip zero-width/invisible
   │
   ▼
detectors run in parallel (sequentially, per-source):
  ├─ RegexDetector (9 patterns, source priority 30)
  ├─ DetectSecretsDetector (12 plugins, source priority 40)
  └─ PiiDetector (Presidio + GLiNER ONNX or spaCy fallback, source priority 20/10)
   │
   ▼  list[Detection]
DetectionResolver.resolve()         3-phase overlap resolution
   │
   ▼  list[Detection]
HeuristicFilter.filter()            entity-type-specific FP rejection
   │
   ▼
DetectionResult(normalized_text, detections)
   │
   ▼
Anonymizer.anonymize(text, detections)   right-to-left replacement with <ENTITY_TYPE>
```

### Entity types (12 constants)

PII (6): `PERSON`, `EMAIL`, `PHONE`, `SSN`, `CREDIT_CARD`, `ADDRESS`.
Secrets (6): `API_KEY`, `PRIVATE_KEY`, `PASSWORD`, `JWT`, `CONNECTION_STRING`, `SECRET`.

### Resolver rules

Source priority: `secrets=40 > regex=30 > pii-gliner=20 > pii-presidio=10`. Three phases:

1. **Exact-span dedup** — identical `(start, end)` collapse, higher priority wins.
2. **Same-source union** — overlapping spans from same source merge into union; compatible nesting (`{(EMAIL, CONNECTION_STRING), (PASSWORD, CONNECTION_STRING), (API_KEY, CONNECTION_STRING)}`) kept separate.
3. **Cross-source overlap** — nested + compatible kept; nested incompatible: higher priority wins; partial overlap different sources: both kept.

### Heuristic filter rejections

Per entity type, the filter rejects known false-positive patterns:

- **PERSON**: <4 char spans, month names, UI keywords (tab/window/help/file/edit/...), ~80 software/app names, filename-with-extension matches.
- **ADDRESS**: Unicode block-drawing/symbol-only spans, VS Code status-bar patterns.
- **PASSWORD**: preceded by `_PATH=` or value looks like a file path.
- **SSN**: not matching `\d{3}-\d{2}-\d{4}` anywhere in span.
- **PHONE**: no separators (- . ( ) + / space).

## Load-bearing invariants

- **Fail-closed default and on error.** `RecorderPrivacyFilter._blocked_reasons["initial"] = inf` at construction. Any exception in `on_window_event()` sets `"filter_error" = inf`. Both clear only on the next *successful* classification.
- **`SCREENCAP_PRIVACY_MODE` env var only tightens.** Loosening via env is silently ignored. The config-file value wins if env would loosen.
- **`SHARED` mode is rejected at parse time.** Defined in the matrix but `parse_privacy_config` raises `InvalidPrivacyConfigError`. MASK_REGION not implemented.
- **`allow_apps` cannot bypass the matrix at the configured mode (matrix-floor invariant).** Originally this was scoped to EXCLUDE only — a password-manager allowlisted by the user was still EXCLUDE via the 5-level precedence. The CLI mutator now enforces a wider floor at write time: `screencap settings privacy allow_apps add <bundle>` is rejected when the bundle's effective class produces EXCLUDE, MASK_WINDOW, or TEXT_REDACT at the configured mode (so chat/email/calendar/video-call apps under `internal` cannot be allow-listed without first changing the mode). Unknown bundles (no `BUNDLE_ID_MAP` entry, no `BROWSER_BUNDLE_IDS` membership, no `app_classes` override) fail closed — the user must classify them first via `app_classes set`. The same severity comparison gates `app_classes set` and `app_classes remove`, so the two-step bypass `set X=password_manager` → `remove X` → `allow_apps add X` is also blocked. The matrix-floor closure is documented in `CHANGELOG.md` under the Unreleased section.
- **`PrivacyConfig.app_classes` is frozen `MappingProxyType`.** Set in `__post_init__`. Mutation attempts raise.
- **`BROWSER_UNVERIFIED` falls through user config override.** Lets domain/keyword refinement still apply.
- **MASK_WINDOW is NOT in `BLOCK_ACTIONS`.** Screenshots ARE captured. They get masked at scrub time. Don't add MASK_WINDOW to `BLOCK_ACTIONS` — it would break the pipeline.
- **`secure_input` gates keystrokes only.** `CGSIsSecureEventInputSet` is system-wide; a background app can set it. Gating screens on it would let any background app block all recording.
- **`secure_field` uses hold timer, not inf.** Set to `now + hold_seconds` (1.0s default). Allowed to expire. Different from `app_policy` which is `inf` while active.
- **1.0s transition hold is mandatory.** Cmd+Tab animations are 200–350ms; the hold must comfortably exceed that.
- **`KEYSTROKE_CONTENT_FIELDS` is the single source of truth.** Both capture-time enforcement (`null_keystroke_content`) and post-capture scrubbing read from this frozenset. Adding a key field that carries content requires updating this set.
- **AuditEntry must be export-safe.** Never put raw text, OCR words, full titles, domains, or query parameters into an audit entry. Use `evidence_type` (category) instead.
- **Detection pipeline operates on normalized text.** All `Detection.start`/`end` offsets refer to NFKC-normalized + zero-width-stripped text. Never pass the original string to `Anonymizer.anonymize`.
- **Detector exceptions don't crash the pipeline.** Errors are recorded in `last_errors` (sanitized — never log input). All-detectors-failed raises `AllDetectorsFailedError`.
- **`SpacyRecognizer` is removed from the registry when GLiNER backend is used.** Otherwise PERSON/LOCATION are double-detected.
- **OCR coordinate convention**: Vision returns normalized bottom-left origin; convert to pixel top-left via Y-axis flip. `_vision_bbox_to_pixels` handles this.

## Before you change it

- Adding a new `ContextClass`: must add an entry for every PrivacyMode column. `_validate_matrix()` will raise at import otherwise. Also wire into `_CONTEXT_REASON` for audit reasons.
- Adding a new entity type: add to `EntityType` constants, add Presidio mapping in `entity_mapping.py:PRESIDIO_MAP`, add GLiNER mapping if applicable, decide if it's compatible-nested with `CONNECTION_STRING`.
- Adding a new bundle ID: add to `BUNDLE_ID_MAP` in `context.py`. For browsers, add to `BROWSER_BUNDLE_IDS`. For sensitive Apple apps, add to `_APPLE_SENSITIVE_APPS` in `app_discovery.py`.
- Changing `KEYSTROKE_CONTENT_FIELDS`: shared by enforcement + scrubber. Check both code paths.
- Changing the resolver source priority: cross-source overlaps will pick differently. Run the benchmark suite (`benchmarks/benchmark_pii.py`).
- Adding a regex pattern: add to `_PATTERNS` in `regex.py`. If it produces `CREDIT_CARD`, add Luhn validation in `detect()`. If high false-positive risk, add a heuristic filter rule.
- Touching `recorder_enforcement.on_window_event`: any uncaught exception will fail-closed the entire recording. Wrap risky additions in try/except and call `fail_closed()` explicitly.

## See also

- [scrubbing.md](./scrubbing.md) — how layer 2 is invoked over a recording
- [recording-engine.md](./recording-engine.md) — how layer 1 gates reach writes
- [export-pipeline.md](./export-pipeline.md) — privacy filter on window switches at export
- [network-capture.md](./network-capture.md) — V1 network capture inherits `mask_domains` for the proxy `ignore_hosts` regex (HTTPS CONNECT layer); does NOT inherit `exclude_apps` (bundle-ID filter, no per-process attribution at the proxy layer); `network/redaction.py` is the single source of truth for capture-time auth/header/query-param scrubs (R10) imported by the addon
- `decisions/` — fail-closed default + env-tightens-only + MASK_WINDOW captures rationale
- `research/` — UT1 blocklist sourcing, GLiNER vs Presidio benchmarks
