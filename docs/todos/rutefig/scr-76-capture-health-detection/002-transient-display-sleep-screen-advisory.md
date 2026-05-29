---
title: "Transient display sleep/lock can surface a spurious screen capture-health advisory"
status: pending
priority: low
created: 2026-05-29
source: code-review SCR-76 (adversarial finding, confidence 70)
related_plans:
  - docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260529-143307-scr76/
---

# Transient display sleep/lock can surface a spurious screen capture-health advisory

## Problem

When the display sleeps, the screen locks, or the display configuration changes,
`utils.take_screenshot()` can return `None` quickly. In `read_screen_events` the
`None` branch increments `screen.attempt` and `continue`s **without a throttle
sleep**, so attempts keep climbing while `screen.output` stays flat. After the
warmup + debounce window this trips the screen stall verdict.

Because the in-process labeller reads **live** TCC state, Screen Recording is
still granted during a display-sleep, so `_probe_tcc_denied("screen")` returns
`None` → the engine emits the advisory `capture_unhealthy(reader=screen)`, not
`permission_lost`. So this is **non-terminal** — a recoverable transient is
mis-reported as a (now self-clearing-on-idle) advisory rather than stopping the
recording.

Note: a *fully blocked* (multi-second) `screencapture` call does NOT trip this —
when the call blocks, `screen.attempt` also stops advancing, so the
`attempt > 0 and output == 0` predicate does not fire. Only the fast-`None`-spin
case does.

## Suggested direction

- Gate the screen verdict on display-active state (e.g. skip the stall check
  while the main display is asleep / locked), or
- Add a short backoff sleep on the `None` branch so a sleeping display does not
  spin attempts without output.

Low priority: advisory-only, and display-sleep recordings are rarely the target
of capture-health concern.

## Verification

- A test simulating sustained `take_screenshot()` → None with Screen Recording
  granted asserts the chosen mitigation (no advisory while display inactive, or
  the backoff applied).
