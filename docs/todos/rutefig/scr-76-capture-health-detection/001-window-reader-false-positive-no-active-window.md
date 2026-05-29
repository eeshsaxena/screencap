---
title: "Window-reader capture-health false positive when there is no active window"
status: pending
priority: medium
created: 2026-05-29
source: code-review SCR-76 (adversarial finding, confidence 80)
related_plans:
  - docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260529-143307-scr76/
---

# Window-reader capture-health false positive when there is no active window

## Problem

The SCR-76 window-reader health verdict treats a falsy `get_active_window_data()`
poll as "attempting but producing no useful output" (unhealthy). But a falsy poll
also occurs for entirely legitimate states where there simply is no focused
window — sitting on the Finder desktop, certain fullscreen apps, or a Space /
Mission Control transition.

If the user stays in such a state for longer than the warmup window plus the
debounce (~13s by default) during an otherwise-healthy recording, the engine
emits `capture_unhealthy(reader=window)` and the shell shows a yellow
"Window capture may not be recording correctly" advisory. This is a false
positive.

It is **non-terminal** (advisory only — the recording continues and nothing is
torn down), so the impact is a misleading hint, not data loss.

## Why it slipped the plan

The plan's design defined window "useful output = `get_active_window_data()`
returned queryable data (truthy)" and assumed `falsy → continue` opens the gap
under Accessibility denial. It did not account for "no active window" being a
normal, benign state that also produces a falsy poll.

## Suggested direction

Distinguish "no active window" (benign) from "Accessibility denied / reader
broken". Options:

- Gate the window unhealthy verdict on the labeller: only surface a window
  advisory when `_probe_tcc_denied("window")` attributes Accessibility denial;
  treat a falsy poll with Accessibility *granted* as a neutral/idle tick.
  (Risk: also suppresses genuine non-TCC window-reader breakage, which is rare.)
- Or track a separate "no active window" signal and exclude it from the stall
  count.

This interacts with the broader action-starvation coverage gap the plan flagged
for a possible brainstorm revisit — consider together.

## Verification

- A test driving the window reader with sustained falsy polls + Accessibility
  granted asserts no `capture_unhealthy(reader=window)` is emitted.
- Genuine Accessibility-denied window stalls still surface (advisory or
  `permission_lost` as appropriate).
