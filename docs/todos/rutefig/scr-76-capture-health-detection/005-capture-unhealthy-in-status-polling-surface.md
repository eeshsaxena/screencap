---
title: "Surface capture_unhealthy on the screencap status polling surface"
status: pending
priority: medium
created: 2026-05-29
source: code-review SCR-76 (agent-native finding, confidence 75)
related_plans:
  - docs/plans/2026-05-29-002-feat-scr-76-capture-health-detection-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260529-150208-deb0faed/
---

# Surface capture_unhealthy on the screencap status polling surface

## Problem

`capture_unhealthy` reaches the SwiftUI shell and any `/v0/events` subscriber with full parity (the daemon's stderr pump publishes every engine line to the EventBus unfiltered). But it is **absent from the polling surface** — `screencap status` / the `/v0/session.snapshot` payload carry no capture-advisory state. An agent (or human) that polls `screencap status` rather than subscribing to the event stream cannot tell that a recording is currently producing nothing useful.

## Why it matters

Agent-native parity: a human watching the menu bar sees the yellow advisory; an agent polling `status --json` does not. The signal is observable via the event stream, so this is a parity gap on the *snapshot/polling* path, not a total blind spot.

## Suggested direction

Add an optional `capture_advisory: {reason, reader} | null` field to the status/snapshot payload, set by the supervisor's event observer on a `capture_unhealthy` edge and cleared on `recording_finalized` (mirror the existing `frames_written`/event-tracking pattern in the daemon supervisor). Alternatively, if polling parity is explicitly out of scope, document in the status schema that `capture_unhealthy` is event-stream-only so agent callers know they must subscribe to `/v0/events`.

## Verification

- `screencap status --json` during a recording with a stalled reader reflects the advisory (or the schema doc states the event-stream-only contract explicitly).
