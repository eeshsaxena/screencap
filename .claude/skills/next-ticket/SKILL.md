---
name: next-ticket
description: Pick the highest-priority ready-to-start Linear ticket on the Screencap team, claim it (assign + move to In Progress / Planning), and hand off into the right Compound Engineering skill (ce-debug / ce-work / ce-plan) primed with the ticket. Flags too-thin tickets with needs-info instead of starting them. Use when you want to start the next piece of work without manually triaging the backlog. Triggers on "next ticket", "what's next", "pick a ticket", "pick the next ticket", "start next work", "grab a ticket", "work the next ticket".
---

# Next Ticket

Pick the next highest-priority, ready-to-start ticket on the **Screencap** Linear team, claim it, and launch the right Compound Engineering skill to work it — flagging tickets that are too thin to start instead of grabbing them.

This is a **launch & hand off** skill: it selects, claims, and routes, then hands control to `ce-debug` / `ce-work` / `ce-plan`. You drive the actual coding from there. It processes **one ticket per invocation**.

All ticket data comes from the Linear MCP connector `plugin:product-management:linear` (verbs: `list_issues`, `get_issue`, `save_issue`, `save_comment`, `list_issue_labels`, `list_issue_statuses`, `get_user`). Scope is the **Screencap team only**. If the connector isn't authenticated, stop and tell the user to authenticate it.

## Reference (Screencap team)

Read these from the project memory `reference_linear_screencap.md` if they look stale; refresh via `list_issue_statuses` / `list_issue_labels` (filtered to the Screencap team) if memberships seem off.

- **Team:** `Screencap` — ID `f3bbac41-0ec3-4f2e-ae0b-96fed6ff624f`
- **Unstarted statuses (eligible):** Backlog `c825fa67-c31e-421c-a5f9-ab2aef7ef5f5`, Todo `cecb19a8-79e3-480c-b4c0-3e19ec8432d5`
- **Started statuses (set on claim):** In Progress `a39dab47-ea30-4a5e-b126-0f4c326de2fc` (work/debug), Planning `86961642-6da6-4d94-9b4c-e87ec902f613` (plan)
- **Labels:** `blocked` `cc921db6-f2c1-43cf-b4c5-da58d0c8ac6d` (excludes a ticket), `needs-info` `f0c57f6c-95cf-4b79-a296-5657d8401bfe` (flag for thin tickets)
- **Priority encoding:** `1`=Urgent, `2`=High, `3`=Medium, `4`=Low, `0`=None. Urgent is highest; **None (`0`) sorts last**, not first.

## Instructions

### Step 1: Query candidate tickets

Fetch unstarted Screencap tickets and build the candidate list.

- Call `list_issues` with `team: "Screencap"` and `state: "unstarted"` (the state *type* matches both Backlog and Todo). Use a generous `limit` (e.g., `100`) and paginate with `cursor` only if needed. If the `state` type filter doesn't behave as expected, fall back to two queries (`state: "Backlog"` and `state: "Todo"`) and merge.
- **Exclude blocked tickets:** drop any candidate carrying the `blocked` label. `list_issues` has no negative-label filter, so do this client-side after the fetch.
- **Order client-side** (`list_issues` only sorts by created/updated, not priority): sort by priority with Urgent→High→Medium→Low first and **None (`0`) last**, then by **oldest-created first** within the same priority band (work the backlog FIFO so old high-priority items don't get buried by newer ones).

If the candidate list is empty, go to Step 3's empty-case handling.

### Step 2: Walk candidates and judge readiness

Take candidates in the order from Step 1. For each one, read its title and description (`get_issue` for full detail) and judge how ready it is to start, using this three-way rubric:

- **Too thin → skip and flag** (Step 3): the goal is unclear — you can't tell what "done" looks like, there's no concrete outcome, scope, or (for a bug) any reproduction detail. A title-only ticket or a vague one-liner lands here.
- **Clear goal but no obvious approach → route to `ce-plan`** (Step 4): you know *what* is wanted, but *how* needs working out — multiple components, an architectural choice, or the ticket itself asks to design / investigate / spike.
- **Clear goal + clear approach → route to `ce-work` or `ce-debug`** (Step 4): actionable enough to start building now.

The first candidate that is **not** too thin is the pick. Stop walking and go to Step 4. Tickets you skipped along the way are handled in Step 3.

### Step 3: Flag thin tickets / handle the empty case

**For each too-thin ticket you skip:**

1. Post a brief comment with `save_comment` (`issueId`, `body`) naming the **observable gaps** — what's missing that would make it startable (e.g., "No acceptance criteria or expected outcome", "Bug report has no reproduction steps"). State only what's missing; do not invent rationale the ticket doesn't contain.
2. Apply the `needs-info` label. **Label writes replace the whole set and must use IDs**, so:
   - Read the ticket's current labels (from the `get_issue` result — these come back as **names**).
   - Resolve each current label name to its canonical **ID** via `list_issue_labels` (prefer the capitalized category IDs — `Bug`/`Feature`/`Improvement` — over the lowercase duplicates).
   - Call `save_issue` with `id: <ticket>` and `labels:` set to the **complete existing ID set + the `needs-info` ID**. Never echo names, never append a partial set (that would drop the ticket's other labels).
3. Continue to the next candidate (back to Step 2).

**If no candidate is eligible** (list empty, or every candidate was too thin): make **no pick**, set no status, and report the backlog state — list the top few tickets and, for each, what it's missing that kept it from being started. Then stop.

### Step 4: Infer the route

For the picked ticket, decide which skill to launch by **reading the ticket** (title + description; pull the comment thread via `list_comments` if it helps for a bug). Do not route off the category label — infer from content:

- **Bug-like → `ce-debug`**: describes broken or unexpected behavior, a regression, an error / stack trace, "doesn't work", or carries reproduction steps. (`ce-debug` is the investigative tool even when the cause is unknown.)
- **Clear goal, non-obvious approach → `ce-plan`**: well-defined outcome but the implementation path needs to be worked out first.
- **Scoped feature / improvement → `ce-work`**: clear goal with an obvious enough path to start building.

Resolve the downstream skill's exact name against the session's available-skills list before the handoff in Step 6 — it may be namespaced (e.g., `compound-engineering:ce-work`). The status to set follows the route: In Progress for `ce-debug`/`ce-work`, Planning for `ce-plan`.

### Step 5: Claim the ticket in Linear

Make these writes the **last board mutations before handoff**, and verify they land:

1. Assign the ticket to the operator and set its status in one `save_issue` call: `id: <ticket>`, `assignee: "me"`, `state: <"In Progress" or "Planning">` (pass the status name or its ID from Reference; match the route from Step 4).
2. **Verify the write succeeded** (check the response). If it failed (auth lapse, connector error), surface the error and **stop — do not hand off** to a CE skill against a ticket whose state didn't actually change.

### Step 6: Heads-up and hand off

1. Emit a brief, **non-blocking** heads-up (then proceed — no confirmation gate):
   - Which ticket (identifier + title) and its priority.
   - Why it's the pick (top eligible by priority; note any thin tickets skipped + flagged).
   - Which skill is launching and why (the route inference from Step 4).
2. **Fire the chosen CE skill now** via the `Skill` tool — do not merely tell the user to type the command. Pass the ticket's context as the free-text argument:
   - `ce-debug` ← the ticket identifier/title + description + relevant comments (reproduction detail).
   - `ce-work` ← the ticket goal + scope.
   - `ce-plan` ← the ticket as the feature description.
3. **Stop.** Process only this one ticket — do not loop back to pick another.

## Handling Args

- `/next-ticket` — pick, claim, and launch the next eligible ticket (default).
- A specific-ticket selector (e.g., `/next-ticket SCR-123`) and a preview/`--dry-run` mode are **not yet supported** — they're deferred follow-ups. If the user passes one, say it's not implemented and run the default flow (or stop, if they wanted dry-run specifically).

## Error Handling

- **Connector not authenticated / unavailable:** stop and tell the user to authenticate `plugin:product-management:linear`. Do not fall back to the deprecated `/sse` Linear server.
- **No eligible ticket:** handled in Step 3 (report backlog state, make no changes beyond any `needs-info` flags).
- **Linear write fails on claim:** handled in Step 5 (surface the error, do not hand off).
- **Downstream skill name doesn't resolve:** report which skill couldn't be launched and the claimed ticket's new state, so the user can launch it manually — don't silently swallow the handoff.
- **Stale IDs:** if a status/label ID from Reference is rejected, refresh via `list_issue_statuses` / `list_issue_labels` (Screencap team) and retry.
