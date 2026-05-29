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
`utils.take_screenshot()` can return `None`. In `read_screen_events` the `None`
branch increments `screen.attempt` and `continue`s **without a throttle sleep**
(the throttle runs only after a successful enqueue), so attempts keep climbing
while `screen.output` stays flat. Once past warmup, `attempt > 0 and output == 0`
trips the screen stall verdict after the debounce.

Because the in-process labeller reads **live** TCC state, Screen Recording is
still granted during a display-sleep, so `_probe_tcc_denied("screen")` returns
`None` → the engine emits the advisory `capture_unhealthy(reader=screen)`, not
`permission_lost`. So this is **non-terminal** — a recoverable transient is
mis-reported as a (now self-clearing-on-idle) advisory rather than stopping the
recording.

**Whether it actually trips depends on the failure mode's speed** (verified
against `_take_screenshot_macos`, `engine/utils.py:126-154`, which runs
`screencapture -x ... -t jpg` with `timeout=10` and returns `None` on any
exception):

- **Fast failure** (e.g. `screencapture` exits non-zero → invalid/empty jpg →
  `Image.open(...).load()` raises → `None` returned in <1s): the `None` branch
  spins with no throttle, so `screen.attempt` advances every loop iteration →
  the gap opens → **trips** after the debounce.
- **Hang to the `timeout=10`**: `subprocess.run` blocks ~10s before raising
  `TimeoutExpired` → `None`. During the block `screen.attempt` does **not**
  advance, so most 1s supervisor ticks see `attempt_delta == 0`, the predicate
  doesn't fire, and the debounce resets → does **not** trip. (Same reason a
  fully-blocked `screencapture` is safe.)

So the open question is empirical: under real display-sleep/lock on the target
macOS, does `screencapture -x` fail fast (trips) or hang/return a lock frame
(safe)? Confirm before relying on either behavior.

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
