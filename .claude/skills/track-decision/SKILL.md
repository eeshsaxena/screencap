---
name: track-decision
description: Record product decisions and requirements from team conversations. Use when new decisions are made, requirements change, priorities shift, or someone says "let's do X instead of Y." Triggers on "track decision", "new decision", "record this decision", "add requirement", "update requirements", "new requirement", "priority change", or when team conversation context contains a product/architecture decision that should be preserved.
---

# Track Decision / Requirement

Record product decisions (ADRs) and requirements from team conversations so rationale isn't lost.

## When to Use

- A team conversation reveals a decision ("let's do X instead of Y")
- New product requirements come in (Slack message, meeting notes)
- Priorities change ("move X to highest priority")
- Someone asks "can you record this decision"

## Instructions

### Step 1: Determine What to Record

Read the conversation context. Classify what's being recorded:

- **Decision** → create/update an ADR in `docs/decisions/`
- **Requirement** → update `docs/REQUIREMENTS.md`
- **Both** → do both (a decision often implies a requirement)

If unclear, ask the user: "Is this a decision (why we chose something) or a requirement (what we need to build), or both?"

### Step 2: Read Existing Docs

Always read before writing to avoid duplicates or contradictions:

```
docs/decisions/          # Existing ADRs
docs/REQUIREMENTS.md     # Current requirements and priorities
```

Check:
- Does an existing ADR already cover this? → update it (or supersede it with a new one)
- Does the requirement already exist? → update priority/description, don't duplicate
- Does this contradict an existing decision? → flag to the user before proceeding

### Step 3a: Create ADR (for decisions)

Find the next ADR number by listing `docs/decisions/`:

```bash
ls docs/decisions/
```

Create `docs/decisions/NNN-short-slug.md` using this template:

```markdown
# NNN — Title (short, imperative)

Date: YYYY-MM-DD
Status: accepted

## Context
What prompted this decision? (2-3 sentences max)
Include who was involved if from a team conversation.

## Decision
What we decided. (1-2 sentences, direct)

## Consequences
What follows from this. (3-5 bullet points)
- What changes
- What stays the same
- What this enables/blocks
- Link to related ADRs if any: [ADR-NNN](NNN-slug.md)
```

Rules:
- Keep it short — half a page max
- Context should capture the *why*, not exhaustive analysis
- Link to related ADRs and tickets where relevant
- If this supersedes an existing ADR, update the old one's status to `superseded by ADR-NNN`

### Step 3b: Update Requirements (for requirements)

Edit `docs/REQUIREMENTS.md`:

- **New requirement** → add to the appropriate priority tier (Highest / Medium / Lower)
- **Priority change** → move the item between tiers, note the date
- **Requirement update** → modify description in place
- **New "Recently Decided" entry** → add at bottom if it came from a team discussion, link to ADR and ticket

Format for each requirement:

```markdown
### Short title
One-paragraph description of what's needed and why.
- **Source:** Who requested, when (if from a conversation)
- **Related:** Links to ADRs, epics, tickets
- **Tickets:** Path to ticket or `—` if none yet
- **Current state:** Brief note on progress (if any)
```

### Step 4: Cross-link

After creating/updating:

- If you created an ADR AND there's a related ticket → add the ADR link to the ticket
- If you updated requirements AND there's a related ADR → link them
- If the decision affects an existing plan → note it in the plan or flag to the user

### Step 5: Confirm with User

Show the user what you created/updated:

```
Recorded:
- ADR-005: [title] → docs/decisions/005-slug.md
- Updated REQUIREMENTS.md: moved "X" to highest priority

Want me to adjust anything?
```

## Handling Args

- `/track-decision` — interactive, reads conversation context
- `/track-decision "we decided to use X because Y"` — create ADR from the quoted text
- `/track-decision requirement "need to support Windows"` — add/update requirement only
- `/track-decision priority "move rewind to highest"` — update requirement priority only

## Examples

**Team says "let's use SQLite instead of Postgres for local storage":**
→ Create ADR explaining context (local-first tool, no server), decision (SQLite), consequences (single-file DB, no concurrent writes)

**Slack message with new feature list and priorities:**
→ Update REQUIREMENTS.md with new items, adjust priorities, add source attribution

**"Don't drop similar screenshots" from product lead:**
→ Create ADR (why: training data needs small visual changes), update requirement (variable-rate capture), link to existing ticket
