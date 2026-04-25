---
name: review-docs
description: Scan docs/ for plans, tickets, research, brainstorms, and decisions, then display them organized by topic. Shows open vs archived counts, item types, and priorities. Use when you want a project overview, need to find existing work, or want to see what's planned. Triggers on "review docs", "show docs", "what's planned", "project overview", "list tickets", "list plans", "docs overview".
---

# Review Docs by Topic

Scan `docs/` for all plans, tickets, research, brainstorms, and decisions, then present them organized by topic with counts and status.

## Instructions

### Step 1: Scan Each Topic Directory

Scan these top-level directories under `docs/`:
- `tickets/` — open tickets
- `plans/` — open plans
- `research/` — research docs
- `brainstorms/` — open brainstorms
- `decisions/` — cross-cutting ADRs
- `tickets/archived/` — archived tickets
- `plans/archived/` — archived plans
- `brainstorms/archived/` — archived brainstorms

Count files in each. For open items, extract the filename to show as a list item.

`docs/solutions/` and `docs/todos/` are also valid topic directories — include them only if the user asks (`/review-docs solutions`, `/review-docs todos`).

### Step 2: Parse Priority from Filenames

Tickets may have priority prefixes in their filenames:
- `urgent-` prefix → URGENT
- `high-` prefix → HIGH
- No prefix → normal

Flag urgent and high items prominently in the output.

### Step 3: Check for Args

Handle optional filtering:
- `/review-docs` — full overview across all topics
- `/review-docs tickets` — show only tickets
- `/review-docs plans` — show only plans
- `/review-docs research` — show only research
- `/review-docs brainstorms` — show only brainstorms
- `/review-docs decisions` — show only decisions
- `/review-docs archived` — show only archived items across all topics

### Step 4: Render Output

Present a structured summary grouped by topic. Format:

```
## Project Docs Overview

### Tickets
Open: N (X urgent, Y high, Z medium, W low, V unset)
Archived: M

**URGENT** `urgent-2026-03-05-fix-pii-false-positives-in-accessibility-text.md`
**HIGH** `high-2026-03-07-audio-no-redaction.md`
**HIGH** `high-2026-03-07-video-no-redaction.md`

Open tickets:
- fix-screenshot-ocr-pii-false-positives (2026-03-05)
- feat-browser-url-via-accessibility-api (2026-03-06)
- feat-scrubbed-copy-auto-export (2026-02-24)
- ...

---

### Plans
Open: N
Archived: M

Open plans:
- fix-scrub-audit-and-hardening (2026-03-03)
- feat-screenshot-image-redaction (2026-03-05)
- ...

---

### Research
Total: N

- redaction-gap-analysis (2026-03-07)
- chatgpt-secret-redaction-deep-research (undated)
- ...

---

### Brainstorms
Open: N
Archived: M

Open brainstorms:
- ...

---

### Decisions
ADRs in `docs/decisions/` — project-wide cross-cutting decisions:
- 001 — Variable-rate capture over dedup gating
- 002 — Disable dhash by default
- 003 — Upload screenshots only, video local
- 004 — No pixel comparison for frame drops

---

### Summary
Total open: X tickets, Y plans, Z research, W brainstorms
Total archived: M items
Decisions: D ADRs
```

### Formatting Rules

- Within tickets, show urgent/high items first, then the rest sorted newest-first
- For each item, strip the date prefix and `-plan`/`-brainstorm` suffix to show a human-readable name
- Show the date in parentheses
- Topics with no items: show the counts line but skip the item list
- Use `---` separators between topics for readability

### Step 5: Offer Follow-up

After showing the overview, offer:
```
Want me to:
- Read a specific doc? (give the filename)
- Show archived items?
- Create a new ticket or plan?
```
