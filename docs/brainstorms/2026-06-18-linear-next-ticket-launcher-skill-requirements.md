---
date: 2026-06-18
topic: linear-next-ticket-launcher-skill
---

# Linear Next-Ticket Launcher Skill

## Summary

A new Linear-native skill that picks the single highest-priority ready-to-start ticket on the Screencap team, marks it in progress (and assigns it), reads it to decide between `ce-debug`, `ce-work`, or `ce-plan`, then hands off into that skill primed with the ticket — while flagging the thin tickets it passes over so the backlog self-improves.

---

## Problem Frame

Starting the next piece of work today is a manual, repetitive ritual: scan the Linear backlog, eyeball priority, judge whether the top ticket is actually actionable, decide whether it's a bug or a feature, move it to In Progress, assign it, then open the right Compound Engineering skill and re-explain the ticket. Each step is small, but together they add friction to the moment that should be the easiest — just starting. The friction also has a quiet cost: thin or under-specified tickets sit at the top of the backlog and get repeatedly re-evaluated instead of being flagged once for triage. The operator wants the glue automated while keeping their own hands on the actual implementation, where judgment matters.

---

## Actors

- A1. Operator: runs the skill when ready for the next ticket, then drives the actual coding once a CE skill opens.
- A2. Launcher skill: selects the ticket, triages thin candidates, sets Linear state, infers routing, and hands off.
- A3. Linear (Screencap team): source of truth for tickets, priority, status, labels, and assignee.
- A4. CE skills (`ce-debug` / `ce-work` / `ce-plan`): receive the primed ticket and carry out the work interactively with the operator.

---

## Key Flows

- F1. Pick and launch (happy path)
  - **Trigger:** Operator invokes the skill.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** Query unstarted, non-blocked Screencap tickets → order by priority → walk down until a sufficiently-detailed ticket is found → read it and infer debug vs work vs plan → assign to operator and set status → emit a brief heads-up → open the chosen CE skill primed with the ticket.
  - **Outcome:** One ticket is owned, in the correct Linear status, and the operator is inside the right CE skill with full ticket context.
  - **Covered by:** R1, R2, R3, R4, R7, R8, R9, R10, R11, R12

- F2. Skip thin / nothing eligible
  - **Trigger:** Operator invokes the skill but the top candidate(s) are too thin, or no candidate is eligible.
  - **Actors:** A2, A3
  - **Steps:** For each too-thin candidate, leave a comment naming what's missing and apply `needs-info`, then continue down priority order → if a later candidate is actionable, fall into F1 → if none are, stop and report.
  - **Outcome:** Either a deeper-but-actionable ticket is launched, or no pick is made and the operator sees the backlog state plus what each top ticket is missing.
  - **Covered by:** R5, R6

---

## Requirements

**Selection & eligibility**
- R1. The skill queries open tickets on the Screencap Linear team and treats as candidates only those in an unstarted status (Backlog or Todo) that are not labeled `blocked`.
- R2. Candidates are ordered by Linear priority (Urgent → High → Medium → Low → No-priority); within the same priority band, the oldest-created ticket is preferred.
- R3. For each candidate in priority order, the skill reads the ticket and judges whether it has enough detail (clear goal/scope) to start. The first sufficiently-detailed candidate becomes the pick.
- R4. The skill selects exactly one ticket per invocation and does not continue to other tickets after handing off.

**Thin-ticket & empty handling**
- R5. When a candidate is too thin to act on, the skill leaves a brief comment naming what's missing and applies the `needs-info` label, then continues to the next candidate.
- R6. When no candidate is eligible, the skill makes no pick and reports the backlog state, including what each top-priority ticket is missing.

**Routing**
- R7. The skill reads the picked ticket's title and description and infers its nature directly, independent of category labels.
- R8. Bug-like tickets route to `ce-debug`; well-scoped feature/improvement tickets route to `ce-work`.
- R9. When the picked ticket has a clear goal but no worked-out approach, the skill routes to `ce-plan` (advises planning) instead of `ce-work`.

**Linear state & handoff**
- R10. Before handing off, the skill assigns the ticket to the operator and sets its Linear status to match the routing: In Progress for work/debug, Planning for plan.
- R11. The skill emits a brief, non-blocking heads-up before handing off — which ticket, why it was picked, and which skill is launching and why — then proceeds without a blocking confirmation.
- R12. The skill opens the chosen CE skill primed with the ticket's context, so the operator can immediately drive the work without re-explaining it.

---

## Acceptance Examples

- AE1. **Covers R5.** Given the highest-priority candidate has only a one-line title and no scope, when the skill evaluates it, it comments naming what's missing, applies `needs-info`, and moves to the next candidate.
- AE2. **Covers R6.** Given every unstarted, non-blocked ticket is too thin, when the skill runs, it makes no pick, applies no In Progress status, and reports each top ticket and what it lacks.
- AE3. **Covers R9, R10.** Given the picked ticket states a clear goal but no approach, when the skill routes it, it sets the ticket to Planning and opens `ce-plan` rather than `ce-work`.
- AE4. **Covers R8, R10.** Given the picked ticket describes a defect with reproduction detail, when the skill routes it, it sets the ticket to In Progress and opens `ce-debug`.

---

## Success Criteria

- Starting the next piece of work collapses from the manual list → judge → assign → set-status → launch ritual into a single invocation, with the operator landing inside the right CE skill.
- The backlog gets cleaner over repeated use: thin tickets accumulate `needs-info` flags and missing-detail notes instead of being silently re-skipped each run.
- The handoff is clean: the launched CE skill opens with enough ticket context that the operator does not re-paste or re-explain, and Linear state (assignee + status) always reflects reality before work begins.

---

## Scope Boundaries

- No full autopilot — the skill never implements end-to-end or opens a PR on its own; the operator drives once a CE skill opens.
- No `ready-for-agent` label gate — eligibility is judged from status + readiness, not a curated label.
- No multi-ticket loop or queue management within a single run — one ticket per invocation.
- Does not touch or replace the file-based `list-tickets` / `docs/tickets/` workflow.
- No rollback / un-pick logic if the operator later abandons the work.
- Screencap team only for v1; other Linear teams are out.

---

## Key Decisions

- Launch & hand off over full autopilot: keeps human judgment in the implementation loop while automating the repetitive glue.
- Read-and-infer routing over label-driven routing: consistent with reading the ticket for readiness, tolerates unlabeled tickets, and unlocks a third "needs planning" path that category labels can't express.
- Flag-and-continue over silent skip: reuses the existing `needs-info` triage label so the backlog improves itself instead of decaying.
- Linear status mirrors routing (Planning vs In Progress): the ticket's state always reflects what is actually about to happen to it.

---

## Dependencies / Assumptions

- The Linear MCP (`plugin:product-management:linear`) is authenticated; the Screencap team, status, and label IDs match the project's Linear reference (Backlog/Todo unstarted statuses, Planning + In Progress started statuses, `blocked` and `needs-info` labels all exist).
- `ce-debug`, `ce-work`, and `ce-plan` are available in-session for handoff.
- The skill lives as a new, separate skill — Linear-native — and is invoked manually by the operator (not scheduled or auto-triggered).

---

## Outstanding Questions

### Deferred to Planning

- [Affects R3][Technical] What concrete heuristic defines "enough detail to start" — i.e., the boundary that separates *skip as thin* from *route to plan* from *route to work*.
- [Affects R12][Technical] The mechanism for opening another CE skill primed with ticket context in-session (direct skill invocation vs. an instruction handoff).
- [Affects R2] Confirm the within-priority tie-break (oldest-created assumed) vs. most-recently-updated — low stakes, can be settled in planning.
