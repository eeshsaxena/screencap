---
title: CGEventTap disabled sentinel causes NSEvent assertion failure in gesture_callback
date: 2026-03-21
problem_type: bug_fix
component: screencap.engine.recorder
platform: macos
severity: medium
symptoms:
  - "unrecognized type is 4294967294"
  - "*** Assertion failure in -[NSEvent _initWithCGEvent:eventRef:], NSEvent.m:1871"
  - "ERROR | screencap.engine.recorder:gesture_callback — Error in gesture event callback"
  - "Gesture events missed for up to 500ms during tap-disabled recovery"
root_cause: gesture_callback calls NSEvent.eventWithCGEvent_() before checking for tap-disabled sentinel event types that AppKit cannot convert
tags: [CGEventTap, NSEvent, gesture, kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput, pyobjc, SecureInput]
files_changed:
  - src/screencap/engine/recorder.py
---

# CGEventTap Disabled Sentinel Causes NSEvent Assertion Failure

## Problem / Goal

During recording, macOS periodically auto-disables the gesture CGEventTap (e.g. when the callback takes too long under GIL contention, or when SecureInput activates for a password field). When disabled, macOS delivers synthetic sentinel event types `kCGEventTapDisabledByTimeout` (0xFFFFFFFE) and `kCGEventTapDisabledByUserInput` (0xFFFFFFFF) to the callback. The `gesture_callback` was calling `NSEvent.eventWithCGEvent_(cg_event)` unconditionally — AppKit cannot map these sentinels to an `NSEventType`, triggering an ObjC assertion failure.

## Solution

Guard the top of `gesture_callback` to detect both sentinel types **before** attempting any NSEvent conversion. When detected, re-enable the tap immediately and return early.

**Key changes:**

```python
# Before (line 1912 — unconditional NSEvent conversion):
def gesture_callback(_proxy, event_type, cg_event, _refcon):
    global _current_pressure, ...
    try:
        ns_event = NSEvent.eventWithCGEvent_(cg_event)  # BOOM on sentinels
        ...

# After — sentinel guard before NSEvent conversion:
def gesture_callback(_proxy, event_type, cg_event, _refcon):
    global _current_pressure, ...

    if event_type in (_TAP_DISABLED_BY_TIMEOUT, _TAP_DISABLED_BY_USER_INPUT):
        logger.debug("Gesture event tap disabled (type=%s), re-enabling", event_type)
        Quartz.CGEventTapEnable(tap, True)  # use `tap` (CFMachPortRef), NOT `_proxy`
        return cg_event

    try:
        ns_event = NSEvent.eventWithCGEvent_(cg_event)
        ...
```

**Files changed:**
- `src/screencap/engine/recorder.py:1870` — Bound `_TAP_DISABLED_BY_TIMEOUT` and `_TAP_DISABLED_BY_USER_INPUT` as local constants in `read_gesture_events()`
- `src/screencap/engine/recorder.py:1916` — Added sentinel guard before `NSEvent.eventWithCGEvent_()`
- `src/screencap/engine/recorder.py:2355` — Added both constants to PyObjC warmup block

## Why This Works

1. **Sentinels are not real events.** `kCGEventTapDisabledByTimeout` (0xFFFFFFFE) and `kCGEventTapDisabledByUserInput` (0xFFFFFFFF) are system notifications delivered unconditionally regardless of the event mask. They tell the callback "your tap was disabled" — they carry no input data. Apple's docs state callbacks must handle them explicitly.

2. **Re-enable is immediate.** The existing `timer_callback` re-enables the tap every 0.5s, but the sentinel guard does it on the spot — reducing gesture event loss from up to 500ms to near-zero.

3. **`tap` not `_proxy`.** `CGEventTapEnable()` takes a `CFMachPortRef` (the tap handle from `CGEventTapCreate`). The callback parameter `_proxy` is a `CGEventTapProxy` — a different type used for `CGEventTapPostEvent`. The existing `timer_callback` already uses `tap` from closure scope.

4. **No race condition.** Both `gesture_callback` and `timer_callback` run on the same CFRunLoop thread (serialized). `CGEventTapEnable(tap, True)` is idempotent, so dual re-enable is harmless.

## Prevention / Gotchas

- **Always handle tap-disabled sentinels in CGEventTap callbacks.** This is a standard macOS pattern — any future event taps must include the same guard.
- **PyObjC warmup is required.** `Quartz.kCGEventTapDisabledByTimeout` triggers lazy bridge resolution on first access. If that happens inside a C callback on the CFRunLoop thread (not the main thread), it's not thread-safe. Always pre-resolve new Quartz constants in the warmup block (~line 2340).
- **`_proxy` vs `tap` confusion.** The callback parameter `_proxy` looks like it should be the tap, but it's a `CGEventTapProxy` — only useful for `CGEventTapPostEvent`. Use `tap` from the enclosing `read_gesture_events()` closure for `CGEventTapEnable`/`CGEventTapIsEnabled`.

## Related

- PR: https://github.com/proteus-computer-use/screencap/pull/114
- Ticket: `docs/tickets/gesture-tap-disabled-crash.md`
- Existing timer re-enable: `recorder.py` lines 2009-2017
