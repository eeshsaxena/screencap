---
name: list-tickets
description: List all open tickets with status and priority. Shows what's pending, in-progress, blocked, or planned. Sorted by priority (urgent > high > medium > low > unset). Use when you want to see the ticket backlog, check what's blocked, find work to do, or get a status overview. Triggers on "list tickets", "ticket status", "what's open", "what needs work", "backlog", "show tickets", "ticket overview", "what's blocked".
---

# List Tickets

Show all open tickets with their status, priority, type, and blockers — sorted by priority.

## Instructions

### Step 1: Scan Open Tickets

List all `.md` files directly in `docs/tickets/`. These are the open (non-archived) tickets. Archived tickets live in `docs/tickets/archived/` — skip those unless the user explicitly asks for them.

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
- `/list-tickets` — all open tickets, sorted by priority
- `/list-tickets high` or `/list-tickets urgent` — filter by priority
- `/list-tickets blocked` — show only blocked tickets
- `/list-tickets in-progress` — show only in-progress tickets
- `/list-tickets feat` or `/list-tickets fix` — filter by type
- `/list-tickets archived` — show archived tickets instead of open

### Step 5: Render Output

Present tickets grouped by priority (urgent first), sorted by date (newest first) within each priority group.

**Priority sort order**: urgent > high > medium > low > unset

Format:

```
## Ticket Status Overview

### URGENT (1 open)

| # | Status | Title | Blocked? |
|---|--------|-------|----------|
| 1 | backlog | Reduce PII false positives in accessibility text | — |

---

### HIGH (3 open)

| # | Status | Title | Blocked? |
|---|--------|-------|----------|
| 1 | open | Audio files have no redaction — only deletion | — |
|   |        | _Partially implemented (2026-03-08): Safety fallback merged via PR #65_ | |
| 2 | open | Video files have no redaction — only deletion | — |
| 3 | backlog | Reduce PII false positives in screenshot OCR | — |

---

### MEDIUM (1 open)

| # | Status | Title | Blocked? |
|---|--------|-------|----------|
| 1 | open | Add screenshots to chunk uploads | Blocked by: video-no-redaction |

---
...

### Summary
Total: N open tickets
By priority: X urgent, Y high, Z medium, W low, V unset
By status: A open, B backlog, C in-progress, D blocked
```

### Formatting Rules

- Sort by priority (urgent > high > medium > low > unset)
- Within each priority group, sort by date (newest first)
- Show the inline status update (if any) as an indented italic line under the ticket
- Blocked tickets: show what blocks them in the last column
- Priority groups with no open tickets: skip entirely
- Priority display: URGENT in bold, HIGH in bold, medium/low/unset in normal case

### Step 6: Offer Follow-up

After showing the overview, offer:
```
Want me to:
- Read a specific ticket? (give the number or name)
- Show archived/completed tickets?
- Create a new ticket?
- Update a ticket's status?
```
