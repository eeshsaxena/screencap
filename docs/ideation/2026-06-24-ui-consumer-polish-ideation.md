---
date: 2026-06-24
topic: ui-consumer-polish
focus: What to add to ScreenCap's macOS UI to be more complete than Day Flow + more user-friendly than the developer-focused Screen Pipe
mode: repo-grounded
---

# Ideation: Consumer-grade UI polish for ScreenCap

**Through-line:** *Day Flow has the story but not the depth; Screen Pipe has the depth but not the story. ScreenCap already has the depth — a local content/transcript/timeline index + MCP, capture-time audit records, redaction evidence, scrubbed cloud copies — but exposes almost none of it as a polished human surface.* So "more user-friendly than Screen Pipe" is mostly **building consumer front doors on infra that already ships**, and "more complete than Day Flow" is **adding the narrative layer + verifiable local-first trust Day Flow lacks**.

## Grounding Context (Codebase Context)

- **Project shape:** Python 3.10+ CLI + LaunchAgent daemon (UNIX-socket `/v0/*` HTTP API, auto-spawn idle-shutdown) + SwiftUI macOS 13+ app shell (`macos/ScreenCap`). Engine is an internal sub-package.
- **Current UI surface:** menu bar (start/stop, sign-in); main window = `NavigationSplitView` with **Calendar / Recordings / Privacy** tabs; month calendar; date-grouped recordings list; per-recording **Review window** (video player + typed-event timeline + redaction-evidence markers + upload/cancel/retry); **Privacy pane** ("Mode: X · read-only in v1" + per-app binary exclude toggles); first-run permissions walkthrough + privacy banner + sign-in; live recording banner (red dot, elapsed time, stop).
- **Latent power not yet surfaced to users:** the daemon already ships `/v0/content.search`, `/v0/transcript.search`, `/v0/timeline.query` + an MCP server (SCR-118), all **local-only, pointer-only, agent-facing** — no human search UI exists. Privacy enforcement already writes structured `AuditEntry`/`ReasonCode` records + a `disable_log` (invisible to the user). The terminal stage already produces a scrubbed/masked cloud copy via `CloudCopyProducer`, distinct from the local-only `recording.db`. Mask primitives + retroactive re-mask already exist.
- **Concrete current-state pain:** recordings-row click shells out to `screencap view` → a **browser tab**, and the stub branch surfaces the literal CLI string "Run `screencap download`" to non-technical users.
- **Bridge/plumbing constraints (past learnings):** SwiftUI drives the Python CLI/daemon over pipes (drain both streams; `readToEnd` on terminate; no timer fork-bombs); TCC permission state caches per-process so **"Quit & Relaunch" is the expected polished pattern** (Loom/1Password precedent); app and daemon are **two separate TCC subjects**; use `Window` not `WindowGroup` for singletons; review-data timing fields are nullable. KB is strong on plumbing, **silent on visual/interaction design**.
- **Product strategy (verbatim approach):** "Win on privacy and performance. Local-first capture with capture-time filtering and per-app consent baked in, low enough overhead to run all day, with rich enough signal (mouse, keyboard, window, network, audio) that the same recording serves the user *and* can be opted into a training corpus for computer-use agents. The consumer product is the wedge; the data flywheel is what makes the strategic case." Active tracks: (1) Capture engine quality & performance — the floor; (2) Privacy & trust; (3) **UX & native experience — load-bearing this quarter**; (4) Replay, MCP & data flywheel. Persona: **non-technical internal-tool operators** (CSMs, ops analysts, sales engineers). Metrics: daily active recording hours, capture reliability ≥98%, CPU/mem p95, privacy incidents/1k recordings, % opting traces into training corpus.
- **External context:** *Day Flow* — AI-narrated activity cards / "git log for your day"; criticized for ~30-min cold start / no time-to-first-value / paywalled features. *Screen Pipe* — powerful but dev-focused (hand-written "pipes," bash-command "proof," **no narrative layer**). *Rewind/Recall* — "ask your history" NL Q&A + timeline scrub + interactive snapshots ("Click to Do"); trust died on **unverifiable** storage / privacy-as-policy. *Adjacent:* capture-vs-decision separation (Rize/Timing), drag-once-teach-a-rule, daily focus score, two-pane what-happened-vs-how-you-label-it (Memtime), proactive surfacing / "On This Day" (Photos Memories), end-of-day review ritual (AI journaling). *Cross-domain:* git-log-as-day-log, flight-recorder/black-box (structural local-only = trust), photo-library-vs-album (archive vs curated story).
- **Additional context:** `2026-06-03-next-work-strategy-ideation.md` (broad five-track prioritization; AX-tree-as-capture-stream #1); `2026-06-03-capture-logs-feedback-ideation.md` (silent failure is the dominant bug; redacted log spine).

## Topic Axes

- **A. Onboarding & first-run trust** — first launch, permissions/TCC, time-to-first-value, "what is this & is it safe," local-only framing.
- **B. Day narrative & timeline** — aggregate storytelling: activity summaries, auto-categorization, day/week review, calendar/list surfaces, proactive surfacing.
- **C. Search & ask-your-history** — user-facing NL Q&A + semantic/keyword search over recordings; the local content-index/MCP power made human-friendly.
- **D. Replay & moment interaction** — per-recording Review window: scrubbing, event drill-down, interactive captured frames, redaction evidence.
- **E. Privacy controls & transparency** — in-product privacy pane, per-app consent tuning, show-before-send, auditable trust signals, redaction-miss feedback, diagnostics.

## Ranked Ideas

### 1. Ask-your-history search bar — "type a question, jump to the moment"
**Description:** A persistent NL/keyword bar at the top of the main window (or a new sidebar item) wired to the already-shipped daemon verbs `/v0/content.search`, `/v0/transcript.search`, `/v0/timeline.query`. Returns ranked **pointer-only** result cards that deep-link straight into the Review window at the right timestamp, with a visible "searches only what's on this Mac" caption. Borrow the librarian discipline: return *you to the moment*, don't *assert* what happened (no hallucinated history).
**Axis:** C
**Basis:** `direct:` — `content_index.py` + the three read-only query verbs + the MCP server already exist, local-only and pointer-only (SCR-118); the SwiftUI app has **no search surface at all** (sidebar is only Calendar/Recordings/Privacy).
**Rationale:** Rewind's signature feature ("ask your history") on the local substrate that fixes the exact trust failure that killed Rewind. The entire user value is gated on one missing UI surface over a finished backend — highest-leverage add in the set.
**Downsides:** NL→query quality needs tuning; results only as good as index coverage (content/transcript are best-effort, not authoritative); risk of over-promising "ask anything."
**Confidence:** 90%
**Complexity:** Medium
**Status:** Explored

### 2. "Your day" narrative home — auto-chaptered cards + inline labels
**Description:** Make the default landing surface a **day story**, not a file list: auto-cluster the day's capture into labeled activity chapters ("~40 min Salesforce + Looker", "1h support tickets") generated locally from `timeline.query`. Left = the machine's guess (what happened); right = a one-tap editable label (how you'd categorize it). Each chapter clicks into replay. Extends to a proactive end-of-day / "On This Day" recap.
**Axis:** B
**Basis:** `external:` Day Flow's AI-narrated "git log for your day" (the layer Screen Pipe lacks) + `direct:` `timeline.query` is `authoritative` for app/window activity, so chapters are computable locally today. Use continuity-based chaptering (audiobook model) to dodge Day Flow's slow/fragile AI-narration cold start.
**Rationale:** The headline "more complete than Day Flow" move — and the compounding gem: every human label correction is a structured annotation over a real screen trace, i.e. the exact supervised signal that makes the opt-in training corpus valuable, produced as a byproduct of normal use.
**Downsides:** Wrong auto-categorization erodes trust fast; needs the correction loop to feel effortless; largest net-new surface.
**Confidence:** 80%
**Complexity:** Medium-High
**Status:** Unexplored

### 3. Show-before-send Outbox — one place that proves nothing leaks
**Description:** For cloud-destined recordings, replace per-recording upload buttons with a single **Outbox**: everything staged to leave the Mac in one holdable queue, each item showing a side-by-side diff of the masked cloud copy vs. the local original (redacted regions highlighted). Resting state is "held locally, nothing has left"; sending is one deliberate, witnessed confirmation.
**Axis:** E
**Basis:** `direct:` the terminal stage already produces a scrubbed/masked cloud copy via `CloudCopyProducer` (distinct from local-only `recording.db`), and the Review window already renders `RedactionEvidenceView` — the artifacts exist on disk. `external:` Rewind/Recall died on *unverifiable* storage; an outbox is the verifiable counter-design.
**Rationale:** Turns the brand's load-bearing promise ("no automatic egress / show before send") from a claim into a screen you can point at — and is the natural moment to ask "contribute this masked trace?" One gate serves privacy transparency *and* the flywheel.
**Downsides:** Only relevant once cloud upload ships (policy = cloud/both); adds a step to the upload path.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 4. Capture-health status + one-button TCC repair — "the light never lies"
**Description:** One always-truthful status (menu bar + window chrome): green "Recording", amber "Needs a quick relaunch to finish granting", red "Paused: permission revoked." Amber owns the macOS Quit & Relaunch dance behind a single guided button that deep-links the right Settings pane. Replaces today's raw failures/alerts.
**Axis:** A
**Basis:** `direct:` the app already has `PermissionWatchdog`, `PermissionController`, `DaemonInstallController`, and surfaces `daemonVersionMismatch` — the signals exist, exposed as errors not one calm status object. Learnings: TCC caches per-process → Quit-&-Relaunch is the expected pattern (Loom/1Password).
**Rationale:** The #1 silent killer of an always-on recorder for a non-technical user is a half-granted/revoked permission they never notice → empty recordings → churn. A single honest health signal converts "my recordings were silently empty" into "it told me to click one button" — protecting the ≥98% reliability metric.
**Downsides:** Must avoid alarm fatigue; two-TCC-subjects (app vs daemon) makes "what's wrong" messaging non-trivial.
**Confidence:** 85%
**Complexity:** Low-Medium
**Status:** Unexplored

### 5. Time-to-first-value onboarding — see your own redacted day in 60 seconds
**Description:** After permissions, immediately offer a guided ~30–60s capture, then drop the user straight into a *populated* Review with a callout: "This is everything we captured — and it never left your Mac." Show where the files live on this disk. Defer sign-in (only for the optional cloud path) so first-run earns trust instead of asking for a password.
**Axis:** A
**Basis:** `direct:` first-run currently ends at granted-permissions → an empty calendar, with sign-in gating unnecessarily (headless/F3 runs fully without it). `external:` Day Flow's documented killer is a ~30-min cold start with no time-to-first-value.
**Rationale:** Time-to-first-value is the biggest onboarding-abandonment lever, and here it doubles as a live privacy demo — the user watches redaction happen on their own pixels before trusting it with a workday.
**Downsides:** A guided capture adds onboarding steps; must handle the "nothing interesting happened in 30s" empty-feeling case.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 6. Interactive native replay — kill the browser shell-out, reach into the moment
**Description:** Make the Review window fully native and interactive: (a) remove the `screencap view` → browser-tab path and the "run `screencap download`" CLI string; (b) add scrubbable interactive snapshots + moment chapters (jump-to-action) + a "chart header" orienting band (apps touched, redactions, upload state); (c) a one-tap "redact this" fix — lasso anything the redactor missed, re-mask it in place, optionally make it a standing rule.
**Axis:** D
**Basis:** `direct:` `RecordingsListView.openInBrowser` shells out to `screencap view` and the stub branch prints "Run `screencap download`"; the Review window already pairs video + typed-event timeline + redaction evidence; mask primitives (`mask_primitives.py`) + retroactive re-mask exist.
**Rationale:** Matches Rewind/Recall's signature interactivity (timeline scrub + interactive snapshots) minus the cloud, and removes the most glaring "is this even a native product yet?" gap. The redaction-miss fix turns the scariest moment (spotting leaked PII) into a reassuring one.
**Downsides:** Interactive-pixel features depend on AX/OCR coverage; native playback is in-flight (v1.1) work.
**Confidence:** 75%
**Complexity:** Medium-High
**Status:** Unexplored

### 7. Live privacy receipt + "0 bytes left this Mac" badge — watch the guard work
**Description:** Turn the read-only Privacy pane into a running, timestamped feed of what capture-time filtering *did* — "Blurred password field in 1Password · Skipped Banking app · Masked 3 emails" — with inline "always exclude this app" on each entry, plus an always-present trust chip: "Nothing has left this Mac · uploaded: 0 B."
**Axis:** E
**Basis:** `direct:` the privacy subsystem already writes structured audit records (`AuditEntry`/`ReasonCode` in `reasons.py`) and a `disable_log`, and `scrub_worker` already propagates retroactive "disable this app" deletes — the data is written but invisible; the pane today is just "Mode: X · read-only in v1" + binary toggles.
**Rationale:** Capture-time filtering is *the* structural-trust differentiator, but if invisible the user takes it on faith. A live feed makes the strongest under-leveraged existing asset (the audit trail) the app's ongoing proof-of-work — and proving non-action ("0 bytes sent") builds as much trust as any feature.
**Downsides:** A noisy feed could overwhelm/alarm; "always exclude from here" needs the drag-once-teach-a-rule discipline to avoid clutter.
**Confidence:** 75%
**Complexity:** Medium
**Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | One-box console (whole app = a text box) | Folded into #1 as a presentation variant |
| 2 | Zero-permission decoy/demo day | Risks feeling deceptive; better as a brainstorm variant of #5 |
| 3 | Spoken/eyes-free audio recap | Novel but narrow; extension of #2, not a core add |
| 4 | Pause-not-stop reframe (no Stop button) | A positioning decision, not a discrete feature — carry into brainstorm as an open question |
| 5 | Mise-en-place permission station | Visual reframing of onboarding; folded into #5 |
| 6 | At-scale "crowded shelf" browser (1000 recordings) | Real but premature; noted as a scaling downside of #2 |
| 7 | Mint-style attention ledger / daily focus score | Overlaps #2 (labels) + #7 (consent feed) |
| 8 | Live capture peek in recording banner | Folded into #7 |
| 9 | Analogy framings (VAR booth, cockpit light, provenance log, chart header, reference desk, audiobook) | Folded into the survivors they inspired (#3/#4/#7/#6/#1) |
