---
name: list-tickets
description: List all open tickets organized by epic with status and priority. Shows what's pending, in-progress, blocked, or planned. Sorted by priority (urgent > high > medium > low > unset). Use when you want to see the ticket backlog, check what's blocked, find work to do, or get a status overview. Triggers on "list tickets", "ticket status", "what's open", "what needs work", "backlog", "show tickets", "ticket overview", "what's blocked".
---

# List Tickets by Epic

Show all open tickets across epics with their status, priority, type, and blockers — sorted by priority within each epic.

## Instructions

### Step 1: Scan All Epics for Open Tickets

For each directory under `docs/epics/*/tickets/`, list all `.md` files. These are the open (non-archived) tickets. Archived tickets live in `docs/epics/*/archived/tickets/` — skip those unless the user explicitly asks for them.

### Step 2: Parse Ticket Metadata

For each ticket file, extract metadata from two sources:

**A. YAML frontmatter** (between `---` delimiters at top of file):
- `title` — ticket title
- `priority` — urgent, high, medium, low
- `status` — open, backlog, in-progress, blocked, done
- `type` — feat, fix, improvement, research, refactor
- `created` or `date` — creation date
- `blocked_by` — filename or path of blocking ticket
- `tags` — list of tags

**B. Filename conventions** (fallback when frontmatter is missing/incomplete):
- Priority prefix: `urgent-` or `high-` in filename
- Date: `YYYY-MM-DD` prefix after any priority prefix
- Type prefix: `feat-`, `fix-`, `refactor-`, `investigate-` after the date

**C. Inline status updates** (in the body, after frontmatter):
- Look for lines matching `> **Status (YYYY-MM-DD):**` — these are the latest status notes
- Extract the most recent one (highest date) as the current status summary
- These override the frontmatter `status` field for display purposes (e.g., frontmatter says "open" but inline status says "Partially implemented")

**Priority precedence**: frontmatter `priority` field > filename prefix > default to "unset"

### Step 3: Detect Blockers

If a ticket has `blocked_by` in frontmatter, or its body contains "Blocked by" with a reference to another ticket, mark it as blocked and note what blocks it.

### Step 4: Check for Args

Handle optional filtering:
- `/list-tickets` — all open tickets across all epics, sorted by priority
- `/list-tickets 01` or `/list-tickets privacy` — filter to a specific epic
- `/list-tickets high` or `/list-tickets urgent` — filter by priority
- `/list-tickets blocked` — show only blocked tickets
- `/list-tickets in-progress` — show only in-progress tickets
- `/list-tickets feat` or `/list-tickets fix` — filter by type

### Step 5: Render Output

Present tickets grouped by epic, sorted by priority within each group.

**Priority sort order**: urgent > high > medium > low > unset

Format:

```
## Ticket Status Overview

### 01 — Privacy & Redaction (6 open)

| # | Priority | Status | Title | Blocked? |
|---|----------|--------|-------|----------|
| 1 | URGENT | backlog | Reduce PII false positives in accessibility text | — |
| 2 | HIGH | open | Audio files have no redaction — only deletion | — |
|   |          |        | _Partially implemented (2026-03-08): Safety fallback merged via PR #65_ | |
| 3 | HIGH | open | Video files have no redaction — only deletion | — |
| 4 | HIGH | backlog | Reduce PII false positives in screenshot OCR | — |
| 5 | HIGH | — | Browser URL via accessibility API | — |
| 6 | MEDIUM | backlog | Auto-export scrubbed copies | — |

---

### 07 — Cloud & Uploads (1 open)

| # | Priority | Status | Title | Blocked? |
|---|----------|--------|-------|----------|
| 1 | MEDIUM | open | Add screenshots to chunk uploads | Blocked by: video-no-redaction |

---
...

### Summary
Total: N open tickets across M epics
By priority: X urgent, Y high, Z medium, W low, V unset
By status: A open, B backlog, C in-progress, D blocked
```

### Formatting Rules

- Sort epics by their numeric prefix (01, 02, 03...)
- Within each epic, sort tickets by priority (urgent first), then by date (newest first)
- Show the inline status update (if any) as an indented italic line under the ticket
- Blocked tickets: show what blocks them in the last column
- Epics with no open tickets: skip entirely (don't show empty epics)
- Priority display: URGENT in bold, HIGH in bold, medium/low/unset in normal case

### Step 6: Offer Follow-up

After showing the overview, offer:
```
Want me to:
- Read a specific ticket? (give the number or name)
- Show archived/completed tickets for an epic?
- Create a new ticket?
- Update a ticket's status?
```
