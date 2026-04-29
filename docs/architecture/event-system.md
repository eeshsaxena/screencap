# Event System

## What it does

Defines the Pydantic event models used everywhere in the project (DB rows, JSONL exports, in-memory pipelines) and the 11-stage processing pipeline that promotes raw hardware signals into semantic events (clicks, drags, typed words, shortcuts).

The event system has no I/O — it's pure transformation. The recorder feeds raw events in; the export pipeline consumes the processed result.

## Pydantic event hierarchy

`BaseEvent` (timestamp + type) is the root. Every concrete event overrides `type` with a `Literal[EventType.X]` constant. `model_config = {"use_enum_values": True}` means JSONL serialization writes the string value directly.

```
BaseEvent
├── Raw mouse events
│   ├── MouseMoveEvent           (x, y, pressure, modifier_flags, path)
│   ├── MouseDownEvent / MouseUpEvent  (x, y, button, pressure, modifier_flags)
│   ├── MouseScrollEvent         (x, y, dx, dy, scroll_phase, momentum_phase, is_continuous)
│   ├── MouseMagnifyEvent        (x, y, magnification)
│   ├── MouseRotateEvent         (x, y, rotation)
│   └── MouseSmartMagnifyEvent   (x, y)
├── Raw keyboard events
│   ├── KeyDownEvent             (key_name, key_char, key_vk + canonical variants)
│   └── KeyUpEvent               (same fields)
├── Screen / audio / window
│   ├── ScreenFrameEvent         (video_timestamp, image_path, width, height)
│   ├── AudioChunkEvent          (start_time, end_time, transcription)
│   ├── WindowStateEvent         (full AX tree snapshot)
│   └── WindowSwitchEvent        (app_name, app_bundle_id, window_title, domain, ...)
└── Derived (post-processing)
    ├── MouseClickEvent          (mouse.singleclick, children: [down, up])
    ├── MouseDoubleClickEvent    (mouse.doubleclick, children: [d1, u1, d2, u2])
    ├── MouseDragEvent           (x, y, dx, dy, button, children: [down, moves..., up, sibling events])
    ├── KeyTypeEvent             (text, children: [downs, ups])
    ├── KeyShortcutEvent         (keys: ["ctrl", "z"], computed text="Ctrl+z")
    └── SpecialKeyEvent          (key_name, computed text="Play")
```

`@computed_field` on `KeyShortcutEvent.text` and `SpecialKeyEvent.text` means they're included in `.model_dump_json()` output despite not being stored fields.

## The 11-stage processing pipeline

`process_events(raw_events)` in `engine/processing.py` runs stages in fixed order. The output is a flat list of merged/promoted events ready for export.

```
raw events
   │
   ▼
1. remove_invalid_keyboard_events       drop keys with no key_name/key_char/key_vk
2. remove_redundant_mouse_move_events   drop dup x/y/pressure/flags consecutive
3. merge_consecutive_keyboard_events    runs of key down/up → KeyTypeEvent or SpecialKeyEvent
3.5. detect_key_shortcuts               KeyType with modifier(s) + non-shift → KeyShortcutEvent
3.75. merge_sequential_key_type_events  adjacent KeyType within 0.5s → merged
4. merge_consecutive_mouse_move_events  compress runs, populate path: list[(x,y)]
5. merge_consecutive_mouse_scroll_events  sum dx/dy
6. merge_consecutive_mouse_magnify_events  sum magnification
7. merge_consecutive_mouse_rotate_events   sum rotation
8. merge_consecutive_mouse_click_events    detect single/double clicks
9. detect_drag_events                      down + moves + up over distance threshold → drag
   │
   ▼
processed events
```

Post-pipeline (called by the exporter, not part of `process_events`):

- `deduplicate_window_events()` — collapses window events on `(app_bundle_id, window_id)`. Title-only changes are suppressed. Late-arriving `browser_url` patches in `domain` via `model_copy()`.
- `interleave_window_events()` — two-pointer merge of action events + window switches by timestamp.

## Thresholds

Module-level constants in `processing.py`:

| Constant | Default | What it gates |
|---|---|---|
| `MOUSE_MOVE_MERGE_DISTANCE_THRESHOLD` | 1 px | Stage 4 |
| `MOUSE_MOVE_MERGE_MIN_IDX_DELTA` | 5 events | Stage 4 |
| `DOUBLE_CLICK_INTERVAL_SECONDS` | 0.5s | Stage 8 |
| `DOUBLE_CLICK_DISTANCE_PIXELS` | 5.0 | Stage 8 |
| `KEY_TYPE_MERGE_INTERVAL_SECONDS` | 0.5s | Stage 3.75 |
| `DRAG_DISTANCE_THRESHOLD` | 3.0 px (env: `SCREENCAP_DRAG_THRESHOLD`) | Stage 9 |

## DB-row → event conversion

`engine/convert.py:dict_to_action_event(row)` is the single dispatch point from raw DB column dicts to Pydantic events. It branches on the legacy `name` column (`"move"`, `"click"`, `"scroll"`, `"press"`, `"release"`, `"magnify"`, `"rotate"`, `"smart_magnify"`). For `"click"` it further branches on `mouse_pressed` to produce `MouseDownEvent` or `MouseUpEvent`.

`dict_to_window_switch(row)` similarly converts a `window_event` row to a `WindowSwitchEvent`. Domain extraction uses `urlparse(browser_url).hostname` with try/except.

Both are used by the chunk processor's raw-sqlite3 path AND `CaptureSession`'s ORM path.

## Input capture

`engine/input.py` wraps pynput listeners:

- `MouseListener` — pynput `mouse.Listener`. `_on_move`/`_on_click`/`_on_scroll` build raw events and call the configured callback.
- `KeyboardListener` — pynput `keyboard.Listener`. Uses `listener.canonical(key)` for layout-normalized variants. Tracks `_stop_sequence_indices` parallel to configured stop sequences.
- `ScreenCapturer` — bg thread (mss-based, used on non-macOS).
- `InputListener` — facade composing mouse + keyboard.

pynput starts its own background thread inside `listener.start()`. Callbacks run on that thread.

## Load-bearing invariants

- **Discriminator is `type`, no Annotated unions.** Concrete classes override `type: Literal[EventType.X]`. There is no `TypeAdapter(discriminator="type")` anywhere — deserialization happens via direct class instantiation, usually from `dict_to_action_event`.
- **`use_enum_values=True`** on `BaseEvent`. JSONL contains `"type": "mouse.singleclick"` not the enum repr.
- **Stage order matters.** Stage 3 (key merge) runs before stage 3.5 (shortcut detect) before stage 3.75 (sequential merge). Reordering changes semantics.
- **`merge_consecutive_keyboard_events` flushes on no-keys-held.** A modifier-down + letter-down without modifier-up will not flush mid-sequence. This preserves Cmd+C-style shortcuts even when mouse events are interleaved.
- **`detect_drag_events` distinguishes drag siblings from drag children.** `MouseDownEvent`, `MouseMoveEvent`, `MouseUpEvent` of the same button are children. Other event types (keys, scrolls, gestures) are siblings: added to drag children AND emitted inline.
- **Already-merged events flush drag state.** `MouseClickEvent`, `MouseDoubleClickEvent` mid-drag terminate the drag; this prevents re-wrapping previously-promoted clicks.
- **`MouseMoveEvent.path` is populated even for single moves.** A solo move gets `path = [(x, y)]`. Code that consumes `path` does not need to special-case the single-move case.
- **Stop sequences are matched on canonical keys**, not raw. Layout-aware so QWERTY-vs-Dvorak users hit the same sequence.
- **`KeyTypeEvent.text` and `KeyShortcutEvent.text` differ by computation source.** `KeyTypeEvent.text` is the field set by stage 3 (concatenated `key_char` from children). `KeyShortcutEvent.text` is a `@computed_field` derived from `keys`. Don't conflate them at write-time.

## Before you change it

- Adding a new event type: define it in `engine/events.py` with `type: Literal[EventType.X]`, add to the `ActionEvent`/`Event` union if applicable, add a `dict_to_action_event` branch if persisted, add a writer-side handler in the recorder.
- Changing stage thresholds: most are module-level; the drag threshold has an env var override. Stage order is harder to change — most stages assume their predecessor's output shape.
- Adding a derived event (post-processing promotion): add a stage function in `processing.py`, wire it into `process_events()` in stage order, decide if its children are `Field(default_factory=list)` and what types nest. The drag children union is the widest example.
- Renaming a field: events are written to JSONL with `model_dump_json()`. Renaming breaks every consumer (viewer, scrubber, Cloud Run, downstream tools). Treat as a breaking schema change.

## See also

- [recording-engine.md](./recording-engine.md) — how raw events reach the pipeline
- [export-pipeline.md](./export-pipeline.md) — how processed events become events.jsonl
- [database.md](./database.md) — how raw events are stored
- [network-capture.md](./network-capture.md) — 5 additional event types (`network.request`, `network.response`, `network.ws_upgrade`, `network.ws_frame`, `network.drop_burst`) live alongside `BaseEvent` but bypass `process_events()` entirely; plus `NetworkPinFailureEvent` (a control-only `pydantic.BaseModel`, NOT a `BaseEvent` — never persisted, never serialized to JSONL)
