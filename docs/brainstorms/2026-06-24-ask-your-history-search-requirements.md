---
date: 2026-06-24
topic: ask-your-history-search
---

# Ask-Your-History Search (in-app, v1)

## Summary

An in-app Search surface in the ScreenCap macOS app: one box that interprets a question with **light local parsing** (time/app/text → filters), runs the **existing on-device index** across all three local streams, and returns **verifiable, pointer-only results plotted on a scrubbable per-day timeline** that jump straight into the Review window. Fully local, no egress, no generated prose. v1 validates the bet on the lowest-lift surface; the global overlay, an on-device intent model, and cited summaries are the documented destination, deliberately out of v1.

---

## Problem Frame

ScreenCap continuously records the screen and already ships a powerful **local** retrieval backend — daemon verbs over an on-device content/transcript/timeline index (SCR-118), today reachable only by agents (MCP), never by the person whose screen it is. The SwiftUI app's sidebar is Calendar / Recordings / Privacy; there is **no way for a human to search their own history at all**. A non-technical operator who remembers *"I saw that error in the vendor portal yesterday afternoon"* can only scroll a date-grouped list and hope.

The two reference points frame the gap. **Rewind** proved consumers want "ask your history," but its natural-language answers shipped retrieved text to a cloud LLM — splitting the product into a trustworthy keyword tier and an untrustworthy "magic" tier, and the privacy promise died on acquisition. **Screen Pipe** has the same local FTS substrate but gates every answer behind BYO-LLM ceremony (pipes, cron files, API keys), which blocks the non-technical user before their first result. ScreenCap already owns the substrate both rely on; what's missing is the human front door and a result experience that is fast, local, and verifiable.

---

## Actors

- A1. **Operator** — the non-technical internal-tool user (CSM, ops analyst, sales engineer) searching their own recorded history on their own Mac.
- A2. **Local retrieval layer** — the on-device daemon query path + content index. Read-only, on-device; returns pointers into recordings, never egresses.

---

## Key Flows

- F1. **Ask and jump to a moment**
  - **Trigger:** A1 opens the in-app Search view and types a query (e.g., "salesforce refund error yesterday afternoon").
  - **Actors:** A1, A2
  - **Steps:** Local parsing extracts time/app/text filters → query runs against the local index across all three streams → results return as ranked pointers → results render as markers on a per-day timeline with recognizable cards → A1 selects one → the native Review window opens at that exact moment.
  - **Outcome:** A1 is looking at the real captured moment in seconds, with zero setup, and nothing left the device.
  - **Covered by:** R1, R2, R3, R4, R5, R6, R7

- F2. **Coverage gap / nothing useful**
  - **Trigger:** A query returns no matches, or returns thin results because on-screen-text indexing is off or a recording isn't indexed yet.
  - **Actors:** A1, A2
  - **Steps:** The surface distinguishes "no matches" from "still indexing" from "this stream isn't covered" → if on-screen-text indexing is disabled, it explains and offers to enable it (with consent) rather than silently underperforming.
  - **Outcome:** A1 understands *why* a result is missing and what to do about it, instead of concluding the feature is broken.
  - **Covered by:** R9, R10

---

## Requirements

**Search surface & input**
- R1. A first-class in-app **Search** destination, alongside Calendar / Recordings / Privacy, with a single free-text input that searches all three local streams (on-screen text, transcript, timeline activity) at once — general recall, not tuned to one use case.
- R2. The input accepts natural phrasing. v1 interprets it with **local, rule-based parsing** of time expressions, app/site names, and free-text terms — no ML model, no network call.

**Retrieval & ranking (v1)**
- R3. Retrieval runs entirely against the **local index** via the existing on-device retrieval path. Results are **pointers** into recordings; the feature never synthesizes prose or makes claims about what happened.
- R4. Results are ordered by **relevance + recency**, not pure chronological order.

**Results & navigation**
- R5. Results render as **markers on a scrubbable per-day timeline** with date navigation; selecting a marker opens the native Review window at that exact timestamp.
- R6. Each result is **recognizable and verifiable**: source app/window, timestamp, and a thumbnail/snippet with the matched text highlighted — enough to recognize the moment without surfacing more than necessary.

**Privacy & trust**
- R7. The surface plainly communicates **local-only** ("searches only what's on this Mac"). No query, result, or captured content leaves the device.
- R8. Results stay **pointer-based**; the search surface does not create new persistent stores of raw captured text beyond what the index already holds (avoids turning search into a fresh exfiltration target — the Recall lesson).

**Coverage & honest states**
- R9. The surface gives **honest coverage states**: distinguish "no matches," "still indexing / not yet indexed," and per-stream coverage differences (timeline is authoritative; on-screen-text and transcript are best-effort).
- R10. Because on-screen-text (OCR) indexing is **off by default** today, v1 must detect that condition and, on the **first text search that would benefit**, present a **one-time consent prompt** explaining full-text search of the screen — enabling indexing only on consent, never silently, and never returning thin results without explanation.

---

## Acceptance Examples

- AE1. **Covers R2, R5.** Given recordings exist for yesterday, when A1 types "vendor portal yesterday afternoon," then the query resolves to a time window + app filter, and matching moments appear as markers on yesterday's timeline.
- AE2. **Covers R3, R6.** Given a matching moment, when A1 selects its result, then the native Review window opens at that timestamp and no generated summary or claim about the moment is shown — only the real captured frame.
- AE3. **Covers R9.** Given a recording whose content is not yet indexed, when A1 searches a term that would match it, then the surface shows a "still indexing" state for that recording rather than a bare "no results."
- AE4. **Covers R10.** Given on-screen-text indexing is disabled, when A1 runs a text search, then the surface explains coverage is limited and offers to enable indexing with consent, instead of returning silently thin results.

---

## Success Criteria

- **Human outcome:** a non-technical operator gets from a vague recollection to the right captured moment in a few seconds, with **zero setup** and no developer ceremony — and can see that it never left their Mac. This is the bar that tells us "ask-your-history" is worth investing further in for ScreenCap.
- **Trust outcome:** every answer is a verifiable pointer to a real moment; there is no path by which search produces an unsourced claim or sends anything off-device.
- **Downstream handoff:** ce-plan has the surface (in-app Search view), the v1 query model boundary (local parsing, no model), the result UX (timeline-anchored, pointer cards, jump-to-Review), the privacy invariants, the coverage/indexing dependency, and the explicitly deferred destination — without needing to invent product behavior.

---

## Scope Boundaries

### Deferred — the destination (sequenced after v1, do not foreclose)
- Global **hotkey / Spotlight-style overlay** that summons search over any app (the highest-reach surface; v1 proves the bet in-app first).
- **One small on-device model** as the NL brain, scoped to **intent extraction only** (question → structured filters), replacing the rule-based parser when warranted.
- An **optional, always-cited "summarize this period"** layer reusing that same model — generated text is only ever shown alongside its source moments; never the default path.
- **Semantic / embedding re-ranking** of results for paraphrase queries (still retrieval, not generation).

### Outside this feature's identity (permanently out)
- Any **cloud LLM or off-device processing** of queries, results, or content.
- **Generated prose answers / synthesized claims** about history (pointer results only — this is what sank Rewind's trust tier).
- Changes to **capture or the privacy/redaction pipeline** — this is a read-only surfacing feature.
- Multi-day **"reconstruct my week" narrative cards** — that is a separate idea (ideation #2, "Your day" narrative home).

---

## Key Decisions

- **In-app surface only for v1; overlay deferred.** The overlay is the higher-reach "magic" surface but also the heavier build (global shortcut + always-available query path + a new window). The in-app view is enough to validate that the retrieval + timeline-result experience delivers value.
- **Light local parsing for v1, not an on-device model.** Prove the retrieval + timeline-result UX with **zero new ML dependency** (no model bundle/runtime/eval surface). The model is the very next step, scoped to intent-only so it never introduces hallucinated history.
- **Pointer-only, no generated prose.** Results *are* the answer. This preserves "no hallucinated history" and is the structural fix for the trust failure that killed Rewind's cloud "Ask."
- **Relevance + recency ranking, not chronological-only.** Screen Pipe's chronological-only ordering is a known weakness for question-driven search.
- **Reuse the existing local retrieval verbs + content index.** This feature is surfacing, not new retrieval infrastructure; the backend already exists and is local-only by rule.
- **OCR indexing stays default-off; enabled via a one-time consent prompt at first text search.** Preserves the privacy-first default-off posture and asks at the moment of felt need — rather than flipping a privacy-relevant default silently or front-loading an onboarding step before the operator wants it.

---

## Dependencies / Assumptions

- **Existing local retrieval backend** — `/v0/content.search`, `/v0/transcript.search`, `/v0/timeline.query` and the local content index exist and are local-only/pointer-only (verified in `src/screencap/daemon/app.py`); v1 builds the human surface on top.
- **On-screen-text (OCR) indexing is default-off** (`content_index`, requires scrub enabled — per CLAUDE.md / SCR-118). This is a **gating dependency** for cross-stream coverage and is the reason R10 exists.
- **Native Review window deep-link target** — results jump into the native Review window; native playback is in-flight (v1.1) work that this feature depends on as the navigation destination.
- Per-stream coverage semantics: timeline is authoritative; content/transcript are best-effort. The surface must represent this honestly (R9).

---

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Technical][Needs research] Scope and locale coverage of the rule-based time/app parser (which time expressions, app-name normalization) — how much "smart feel" the parser delivers before the intent model lands.
- [Affects R4][Technical] The exact relevance+recency blend across an authoritative stream (timeline) and best-effort streams (content/transcript).
- [Affects R5][Technical] Per-day timeline rendering and navigation performance as recording volume grows (the at-scale "crowded shelf" concern, noted but not designed here).
