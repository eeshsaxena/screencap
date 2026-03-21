---
name: review-docs
description: Scan docs/ for plans, tickets, research, brainstorms, and decisions, then display them organized by epic. Shows open vs archived counts, item types, priorities, and orphaned docs outside epics. Use when you want a project overview, need to find existing work, or want to see what's planned across all epics. Triggers on "review docs", "show docs", "what's planned", "project overview", "list tickets", "list plans", "epic overview", "docs overview".
---

# Review Docs by Epic

Scan `docs/` for all plans, tickets, research, brainstorms, and decisions, then present them organized by epic with counts and status.

## Instructions

### Step 1: Gather All Epics

List all directories under `docs/epics/`:

```bash
ls docs/epics/
```

For each epic, read its `README.md` to get the title and description.

### Step 2: Scan Each Epic

For each epic directory, scan these subdirectories:
- `tickets/` — open tickets
- `plans/` — open plans
- `research/` — research docs
- `brainstorms/` — brainstorm docs
- `archived/tickets/` — archived tickets
- `archived/plans/` — archived plans
- `archived/brainstorms/` — archived brainstorms
- `archived/research/` — archived research

Count files in each. For open items, extract the filename to show as a list item.

### Step 3: Gather Decisions (Cross-Cutting)

Decisions live in `docs/decisions/` and are NOT scoped to any epic — they are cross-cutting ADRs. List all decision files and show them in their own top-level section (not under any epic, not as "orphaned").

### Step 4: Scan Orphaned Docs

Check for docs outside the epics structure:
- `docs/plans/` — plans not assigned to any epic
- `docs/todos/` — todo files from branch work

### Step 5: Parse Priority from Filenames

Tickets may have priority prefixes in their filenames:
- `urgent-` prefix → URGENT
- `high-` prefix → HIGH
- No prefix → normal

Flag urgent and high items prominently in the output.

### Step 6: Check for Args

Handle optional filtering:
- `/review-docs` — full overview of all epics
- `/review-docs tickets` — show only tickets across all epics
- `/review-docs plans` — show only plans
- `/review-docs research` — show only research
- `/review-docs 01` or `/review-docs privacy` — filter to a specific epic (by number or keyword in name)
- `/review-docs orphaned` — show only docs outside the epics structure

### Step 7: Render Output

Present a structured summary. Format:

```
## Project Docs Overview

### 01 — Privacy & Redaction
Open: 6 tickets, 3 plans, 4 research, 1 brainstorm
Archived: 37 items

**URGENT** `urgent-2026-03-05-fix-pii-false-positives-in-accessibility-text.md`
**HIGH** `high-2026-03-07-audio-no-redaction.md`
**HIGH** `high-2026-03-07-video-no-redaction.md`

Tickets:
- fix-screenshot-ocr-pii-false-positives (2026-03-05)
- feat-browser-url-via-accessibility-api (2026-03-06)
- feat-scrubbed-copy-auto-export (2026-02-24)

Plans:
- fix-scrub-audit-and-hardening (2026-03-03)
- feat-screenshot-image-redaction (2026-03-05)
- privacy-v3-phase-6-benchmarks-hardening (2026-03-06)

Research:
- redaction-gap-analysis (2026-03-07)
- chatgpt-secret-redaction-deep-research (undated)
- ...

---

### 02 — Recording Pipeline
Open: 0 tickets, 0 plans, 0 research
Archived: 5 items
(no open items)

---
...

### Decisions (Cross-Cutting)
ADRs in `docs/decisions/` — these are project-wide, not scoped to any epic:
- 001 — Variable-rate capture over dedup gating
- 002 — Disable dhash by default
- 003 — Upload screenshots only, video local
- 004 — No pixel comparison for frame drops

---

### Orphaned Docs
Plans not in any epic:
- docs/plans/2026-03-09-feat-variable-rate-capture-action-type-gating-plan.md

### Summary
Total open: X tickets, Y plans, Z research, W brainstorms across N epics
Total archived: M items
Decisions: D ADRs (cross-cutting)
Orphaned: P docs outside epics
```

### Formatting Rules

- Sort epics by their numeric prefix (01, 02, 03...)
- Within each epic, show urgent/high items first, then tickets, then plans, then research, then brainstorms
- For each item, strip the date prefix and `-plan`/`-brainstorm` suffix to show a human-readable name
- Show the date in parentheses
- Epics with no open items: show the counts line but skip the item list
- Use `---` separators between epics for readability

### Step 8: Offer Follow-up

After showing the overview, offer:
```
Want me to:
- Read a specific doc? (give the filename)
- Show archived items for an epic?
- Create a new ticket or plan?
```
