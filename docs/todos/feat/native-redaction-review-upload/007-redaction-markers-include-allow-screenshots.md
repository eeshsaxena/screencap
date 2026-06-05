---
title: "P2: redaction timeline markers include clean/ALLOW screenshots (over-reports protection)"
status: open
priority: medium
created: 2026-06-05
source: code-review (ce-code-review, finding correctness #1 / F4)
related_plans:
  - docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260605-112111-bcc9fab3/
---

# P2: per-moment redaction markers fire on un-redacted frames

## Problem

`mask_screenshots` (`src/screencap/scrubber.py` ~:2016) appends an `AuditEntry`
for **every** screenshot it processes, including `PrivacyAction.ALLOW` (clean)
frames. `_build_redaction_evidence` (`src/screencap/review.py` ~:136) maps **all**
`audit_entries` into per-moment `{t, category}` markers with no action filter:

```python
"markers": [
    {"t": e.timestamp, "category": e.reason}
    for e in scrub_result.audit_entries
],
```

So on any recording with screenshots, the review timeline draws a "redaction"
tick at essentially every frame — misrepresenting the R8 evidence (the operator
sees "protected" markers where nothing was redacted). This is an evidence-display
correctness/UX bug, **not** a data leak (markers carry timestamp + category only,
never a value).

Existing tests don't catch it: their fixtures have no `screenshots/` dir, so
`mask_screenshots` returns early and never emits ALLOW entries.

## What's needed

- Exclude clean entries from markers — filter `e.action == PrivacyAction.ALLOW.value`
  (or the equivalent) in `_build_redaction_evidence`, or stop emitting ALLOW
  `AuditEntry`s for the marker channel while preserving them where genuinely
  needed (e.g. coverage/telemetry).
- Decide whether ALLOW screenshot entries should still count toward the summary
  tally (today `summary` uses `entity_counts`, which is separate — confirm it
  isn't also inflated).
- Add a `real_scrub`-marked test with an ALLOW screenshot asserting that frame
  is **not** present in `redaction.markers`.

## Why deferred

Behavior-changing to the evidence payload (and wants a `real_scrub` fixture that
exercises `mask_screenshots`); left for a deliberate fix rather than auto-applied.
