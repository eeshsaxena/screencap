---
date: 2026-06-03
topic: capture-logs-feedback
focus: How to capture logs and feedback from the users
mode: repo-grounded
---

# Ideation: Capturing logs & feedback from ScreenCap's users

The through-line: ScreenCap's **dominant documented bug is silent failure**, and its persona is **non-technical**, and its brand is **privacy/local-first**. So the whole topic is "convert invisible machine and user signal into observable, privacy-safe, low-friction data" — ideally over **one shared substrate** rather than five bespoke pipes, with **zero automatic egress** as an architectural invariant.

## Grounding Context (Codebase Context)

- **Project shape:** macOS-only Python `click` CLI (`screencap`) supervised by a background daemon (LaunchAgent + UNIX-socket `/v0/*` HTTP API), plus a SwiftUI macOS app shell. Engine is an internal sub-package.
- **Logging today:** every module uses stdlib `logging.getLogger(__name__)` but there is **no centralized logging config and no `RotatingFileHandler`** — logs scatter and spawned engine-worker logs can vanish (no root handler in children).
- **Existing sinks:** `~/.screencap/run/auto-serve.log` (mode 0o600); `~/Library/Logs/ScreenCap/daemon.{out,err}.log` (LaunchAgent stdout/stderr); `~/.screencap/menubar_debug.log`; per-recording `.recording_error.log` + `.recording_ready` sentinels and network `.mitmdump.log`; per-recording `system_metrics.json` (schema v4: platform/network/psutil). Recordings live at `~/.screencap/recordings/<name>/`.
- **Reusable substrate already present:** daemon EventBus + `/v0/events?since=cursor` replay + `/v0/status`; opt-in GCS upload pipeline being built.
- **No** `doctor` / `feedback` / `bugreport` / `telemetry` / `crash-report` command exists today. Sentry is **not** a dependency (it appears only in privacy blocklists).
- **Dominant in-repo bug shape = SILENT FAILURE:** `except Exception: pass` (including on the error-logger itself at `session.py:242`), success inferred from partial state, "Recording complete" shown over destroyed data, install verification piped to `/dev/null` hiding a SIGKILL, mitmproxy "0 flows" indistinguishable from success. North star for this topic: **make absence observable; fail closed.**
- **Constraints from prior learnings:** launchd does not expand `~` in plist log-path keys (use absolute paths, pre-create dirs); drain subprocess stdout+stderr to EOF or lose the final crash line; the privacy pipeline (Presidio/spaCy/fast-gliner) can fail to init silently.

**STRATEGY alignment:** persona = non-technical internal-tool operators (CSMs, ops analysts, sales engineers); approach = win on privacy + performance, local-first, per-app consent, opt-in training-corpus flywheel. KPIs = daily active recording hours, capture reliability ≥98%, CPU/mem p95, **privacy incidents per 1k recordings**, % of users opting traces into the training corpus. The "UX & native experience" track is load-bearing this quarter.

**External context — privacy-first app prior art (round 2):**
- **Redact-at-write, not redact-at-send** (Mullvad, Signal). Signal iOS #2540 proved regex-scrub-at-upload leaks full phone numbers — redaction must happen at log-emission time.
- **Show-before-send is a hard requirement** (Mullvad CLI/in-app; Firefox crash dialog "Details…"): user reads exact bytes before the irreversible upload.
- **Never send automatically; no unconditional pings, no persistent install ID** (Signal/Mullvad send zero auto-telemetry; Firefox's uncancellable DAU ping was a multi-year trust wound). Minimal version-check pattern = Mullvad's two headers (app version + OS-major, once/24h, no device ID).
- **Brave P3A / STAR + Apple local differential privacy:** bucket values into coarse bins, randomized response on-device, server-side k-anonymity (k=50) → **no per-session records ever leave**; even a compromised server has nothing per-user to leak.
- **Two-category telemetry split** (Zed: `diagnostics` vs `metrics`, independently togglable) beats a single collapsed toggle.
- **Obsidian "copy-to-clipboard sanitized debug info, paste into issue"** — account-free, zero upload, zero attack surface; the right v1 of any diagnostics feature.
- **Little Snitch Internet Access Policy (IAP):** a machine-readable manifest of every outbound endpoint turns privacy claims into auditable facts.

## Topic Axes

- **A. Log capture & retention** — the local log sinks: unify, structure, rotate, retain, redact-at-write.
- **B. Diagnostics bundle & support handoff** — `doctor`/`bugreport` on-demand collection with explicit include/exclude transparency and show-before-send.
- **C. Crash & error reporting** — converting silent failures into observable signals; unhandled-exception / daemon-crash capture; fail-closed surfacing.
- **D. Usage telemetry & consent** — opt-in metrics tied to KPIs, consent UX, transport/destination, opt-out, auditable egress.
- **E. In-product user feedback** — bug reports, feature requests, "scrub missed something" / quality signals, routing.

Privacy/redaction-before-egress is a cross-cutting constraint threaded through all five (it's a screen recorder).

## Ranked Ideas

> **Recommended "fund-this" set (5):** #1 (redacted substrate) → #2-v1 (clipboard `debug-info`, cheapest win) → #3 (kill silent failure / reliability KPI) → #8 (bucketed-DP telemetry, resolves the Sentry/PostHog question) → #5 (scrub-miss feedback instrument). #9 (IAP manifest) is a near-free differentiator bolt-on; #7 (BYO-endpoint) is held as the *enterprise* telemetry option behind #8; #4 supplies the consent UX over whichever telemetry spine wins; #6 is the continuous-capture upgrade once #1/#3 exist.

### 1. One redacted, structured log spine — the substrate everything else reads from
**Description:** Replace the ~30 scattered `getLogger(__name__)` calls (no handler config today) with a single `screencap.logging.configure()` that installs a `RotatingFileHandler` + `QueueHandler` writing **structured JSONL** to one rotated log, tagged by `source`/`recording_id`/`pid`. Every line passes through a `RedactingFormatter` that scrubs home paths→`~`, recording names→stable hashes, hostnames/usernames **at emit time**. Make this envelope the thing doctor, telemetry, crash, and feedback all read from (reuse the EventBus cursor as in-process transport). The existing sink files become filtered views of one spine.
**Axis:** A — Log capture & retention
**Basis:** `direct:` grounding confirms "NO centralized logging config or RotatingFileHandler"; scrubbing exists only in `scrubber.py` for *recordings*, never logs; the daemon already has an EventBus + `/v0/events?since=cursor`. `external:` Mullvad/Signal both redact at write time — Signal iOS #2540 is proof that redact-at-send leaks.
**Rationale:** Load-bearing prerequisite. Spawned-worker logs can vanish today; unbounded sinks threaten the disk/p95 story. Once one structured, redacted spine exists, every other idea becomes a thin view — the compounding payoff — and redaction-at-write is the only correct place to do it.
**Downsides:** Touches every module's logging; multiprocess QueueHandler wiring is fiddly; redaction-at-emit can over-scrub and make logs harder for engineers to read.
**Confidence:** 90%
**Complexity:** Medium
**Status:** Unexplored

### 2. `screencap doctor` / `debug-info` — a maturity ladder from clipboard to consented upload
**Description:** A diagnostics handoff that grows in three rungs:
- **v1 — `screencap debug-info`:** a sanitized, human-readable snapshot (versions, platform, codec, daemon state, log tail, redaction-class summary) copied to clipboard or a local file; the user pastes it into a GitHub issue. **Zero upload, zero attack surface** (Obsidian pattern).
- **v2 — `doctor` + `doctor --bundle`:** read-only `doctor` renders **human verdicts** ("Screen Recording permission: OFF — open System Settings › Privacy"), pattern-matching known silent-failure signatures (unexpanded `~` in plist, privacy-pipeline init failure, stale CLI vs daemon, TCC not granted). `--bundle` writes one local zip through the redaction gate with an explicit include/exclude manifest, **account-free**, applying a Mullvad-style redaction list (account IDs, home dir, IPs, UUIDs), and **shows the content before any send**.
- **v3 — optional consented upload** returning a short **marker-ID** URL the user pastes into a ticket (Signal/Tailscale).
**Axis:** B — Diagnostics bundle & support handoff
**Basis:** `external:` Obsidian (clipboard snapshot), Mullvad (`mullvad-problem-report` collect→inspect→send, redaction list, account-free), Firefox ("Details…" show-before-send), tailscale/Screen Studio (marker-ID, explicit include/exclude). `direct:` sinks are scattered across 3+ locations and there is no `doctor` command today.
**Rationale:** The persona is non-technical — telling a CSM to paste `daemon.err.log` is a UX failure. v1 is the highest-confidence, cheapest first ship with no privacy surface; v2/v3 add capability only as trust/infra mature. Directly serves the load-bearing "UX & native" track.
**Downsides:** The rules table needs maintenance; the include/exclude boundary is a privacy decision that must be settled before v2/v3; show-before-send adds a UI surface.
**Confidence:** 90%
**Complexity:** Medium (v1 Low)
**Status:** Unexplored

### 3. Make silent failure impossible — fail-closed outcome verdict + loud crash capture
**Description:** (a) Every recording gets a fail-closed `outcome.json` — `{status: ok|degraded|failed, evidence:[...], missing:[...]}` computed by assertions (frames>0, files non-empty), so a recording can't be "complete" unless it proves it captured something. (b) Install `sys.excepthook` + `atexit` + signal handlers in daemon/engine workers that drain stdout/stderr to EOF, write a **fingerprint-keyed** crash envelope (dedup at capture), and emit a `daemon.crashed` event onto the EventBus → menubar error state. (c) A ruff lint rule banning `except Exception: pass` and `>/dev/null` swallowing. (d) Stable **OBD-II-style error codes** (`SCR-WRITER-STALL-002`) with a public lookup.
**Axis:** C — Crash & error reporting
**Basis:** `direct:` in-repo learnings name silent failure as the dominant bug — `except:pass` (incl. on the error-logger at `session.py:242`), success-from-partial-state, "Recording complete" over destroyed data, `/dev/null`-hidden SIGKILL. `external:` Sentry's excepthook/atexit + on-disk envelope model applied *locally* (Sentry deliberately not a dependency).
**Rationale:** **Capture reliability ≥98% is a headline KPI and is literally uncomputable while failures are silently swallowed.** Single highest-leverage change for the reliability number; loud-crash path reuses the existing EventBus + menubar so it's cheap. Computes the KPI with zero egress.
**Downsides:** Defining the worker↔controller success/failure contract is real design work; over-eager "degraded" verdicts could cry wolf.
**Confidence:** 82%
**Complexity:** Medium-High
**Status:** Unexplored

### 4. Opt-in tiered telemetry consent — the "whether and what" UX
**Description:** A `settings telemetry` control with a **Zed-style `diagnostics` vs `metrics` split** (independently togglable) plus tiers (off / crash / health / all), default **off**, a live `telemetry --show` glass box that prints exactly what each tier would send, a published "what we do NOT collect" block, and `DO_NOT_TRACK` honored. Bind consent to the **existing per-app consent rail** rather than a new prompt, backed by an append-only **consent ledger** (revocable, receipted, shown in settings) that every egress checks and stamps — keeping "reliability metrics" and "training-corpus contribution" as *separate* consents. No unconditional pings; no persistent install ID.
**Axis:** D — Usage telemetry & consent
**Basis:** `external:` Homebrew consent-before-send, VS Code/Zed levels & split, Next.js "what we do NOT collect", GitHub-CLI/Firefox-DAU backlash → `DO_NOT_TRACK` + no unconditional ping, clinical-trial tiered/revocable informed consent. `reasoned:` the strategy lists hard KPIs that have *no transport* today, yet a privacy-first product that gets consent wrong torches its positioning.
**Rationale:** This is the consent *UX layer*; it composes with whatever telemetry spine wins (#8 default, #7 enterprise). The ledger makes the training-corpus flywheel ethically/legally defensible (provable per-trace provenance).
**Downsides:** Highest product-judgment risk; ledger + per-app binding is non-trivial; even respectful telemetry invites scrutiny for a screen recorder.
**Confidence:** 75%
**Complexity:** Medium-High
**Status:** Unexplored

### 5. Timeline-pinned "scrub missed something" — the privacy-incident KPI made measurable
**Description:** Reframe feedback from a separate command into a **one-key reaction pinned to a timeline moment**: while reviewing a recording (or right after a failure), the user flags "scrubber missed this" / "shouldn't have been captured" / "wrong app detected" at a timestamp. Special-case the missed-redaction path so it *is* the instrument for "privacy incidents per 1k" — capturing only marker-ID + region + redaction-class + consent stamp (never the leaked pixels unless explicitly opted in), routing to the redaction backlog and, with consent, the corpus. Optionally auto-detect: when post-scrub OCR still finds probable PII, emit the signal without the user typing.
**Axis:** E — In-product user feedback
**Basis:** `reasoned:` "privacy incidents per 1k recordings" is a named KPI but the product has no mechanism to observe an incident — a missed redaction is invisible unless the user flags it, and is meaningless without the exact moment the recording already timestamps. `external:` NASA ASRS blameless near-miss reporting; restaurant point-of-experience comment cards. `direct:` `scrubber.py`/`privacy/filter.py`/`pii.py` are the natural emission points; the review screen is already in `docs/brainstorms/`.
**Rationale:** The highest-value feedback for a privacy-first recorder is "you leaked something" — turning user annoyance into the exact labeled signal that hardens the scrubber and feeds the flywheel. Closes a measurement gap on a headline KPI.
**Downsides:** Depends on the upload-review-screen surface landing; auto-attaching the flagged recording means handling the very PII that leaked; risk of alert fatigue if over-prompted.
**Confidence:** 78%
**Complexity:** Medium
**Status:** Unexplored

### 6. Flight-data-recorder ring buffer — capture the 90 seconds *before* it broke
**Description:** Keep a continuous bounded ring buffer of structured envelopes (last N min / M MB) in the daemon and per-recording, plus a low-rate "capture vitals" heartbeat (frames/sec, queue depth, last-event ts) so a stalled writer shows as a visible *flatline*. On any failure/crash/user-report, snapshot the buffer slice around a marker-ID **at fault time** — `doctor` then lists pre-built incidents instead of scraping live logs. Optionally aggregate weak signals (rising retries, growing latency) into a "this machine is trending wrong" status before anything hard-fails.
**Axis:** B — Diagnostics bundle & support handoff (continuous-capture cut)
**Basis:** `external:` tailscale's "continuous breadcrumbs + on-demand marker-ID" and the aviation flight-data-recorder model (both cited as more robust than collect-everything-on-demand); epidemiological syndromic surveillance for the trend layer. `direct:` the in-repo learning that "the final crash line is routinely lost" — if you only collect on demand, that line is already gone.
**Rationale:** Makes #2 and #3 actually *have data*: the context that explains a failure is captured before the user notices. The heartbeat directly attacks the "absence-of-signal" north star and feeds reliability + p95 KPIs from real per-session data.
**Downsides:** Continuous buffering has its own CPU/mem/disk cost (must stay under p95 budget); overlaps with #1 and #3 — needs careful boundary-drawing.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 7. Pluggable, consent-gated observability exporter with BYO-endpoint
**Description:** Treat "where telemetry lands" as a swappable exporter over the #1 substrate, with three implementations: **local-only** (default — data never leaves; KPIs computed on-device), **dogfood** (internal/dev builds export crashes to the team's own Sentry and usage to the team's own PostHog — both hard-off in distributed builds), and **BYO-endpoint** (`telemetry --endpoint <dsn/url>` lets a self-hosting customer route anonymized, redaction-gated events into *their own* Sentry/PostHog/OTel stack). Everything passes through the #1 redaction gate first; PostHog session-replay is permanently disabled.
**Axis:** D — Usage telemetry & consent (egress architecture; carries C/D/B)
**Basis:** `external:` Sentry and PostHog are both self-hostable; the GitHub-CLI silent-opt-out backlash shows default-on third-party telemetry torches trust for a privacy brand. `reasoned:` for a local-first tool the SDK's value *is* the aggregation backend, so the only question that matters is the destination — making it pluggable defers and localizes that decision; for a regulated/enterprise ICP, "route to *your* stack" is a procurement requirement.
**Rationale:** Full observability of the team's own fleet immediately (dogfood) while shipping zero-egress-by-default to customers; gives enterprise buyers a clean "send to our own stack" answer that passes security review. Keeps the cloud-vs-self-host-vs-local decision reversible forever.
**Downsides:** A pluggable exporter framework + per-build gating + consent + redaction integration is real engineering; multi-backend adapters carry ongoing cost; "dev-only" gating must be enforced at build time or a leaked default-on exporter becomes the exact trust failure being avoided. **Note:** #8 (bucketed-DP) is strictly more privacy-defensible than this and may be the better *default*, with #7 reserved for consenting enterprises.
**Confidence:** 80%
**Complexity:** High
**Status:** Unexplored

### 8. Bucketed, differentially-private usage telemetry — no per-session records, ever
**Description:** Collect usage as coarse histogram bins (recording duration: 0–1m / 1–5m / 5–30m / 30m+; reliability: ok/degraded/failed; CPU/mem buckets) with **randomized response applied on-device** before anything leaves, a random send delay, and server-side k-anonymity. The backend only ever sees histograms — never a per-user event.
**Axis:** D — Usage telemetry & consent
**Basis:** `external:` Brave P3A (bucketing + randomized response) and STAR (Shamir secret sharing + VOPRF, k=50); Apple's local DP (Count Mean Sketch / Hadamard Response). `reasoned:` strictly stronger than #7 for the privacy brand — population-level KPIs with *nothing to leak* even under server compromise.
**Rationale:** Answers the strategy's hard KPIs (reliability rate, recording-hour distribution, % opting into corpus) at the population level **without a surveillance system and without a third-party dependency** — the cleanest resolution to the Sentry/PostHog question, because it removes the "per-user data leaves the machine" premise entirely.
**Downsides:** Real statistical/crypto engineering (randomized-response tuning, epsilon budget); loses per-user debugging *by design*; k-anonymity is weak at a low install base; harder to explain to stakeholders than a PostHog dashboard.
**Confidence:** 72%
**Complexity:** High
**Status:** Unexplored

### 9. Auditable egress — a machine-readable Internet Access Policy
**Description:** Ship a machine-readable manifest declaring **every endpoint the app can ever contact**, its purpose, and the block-consequence — paired with the redaction-on-egress gate (#1) so the manifest is *provably complete* (the gate is the sole egress; the manifest enumerates it). Tools like Little Snitch auto-verify it.
**Axis:** D/B — trust surface
**Basis:** `external:` Little Snitch's Internet Access Policy (IAP) XML. `reasoned:` the ICP — operators on locked-down corporate machines — are exactly the people already running network monitors; this turns "we're private" from a claim into an auditable fact.
**Rationale:** Near-zero cost, strong differentiator vs. Screen Studio and others who assert privacy without substantiating it. Makes whatever telemetry choice lands (#7/#8/local) transparent rather than discovered.
**Downsides:** Only meaningful to a sophisticated sub-segment; a stale manifest is worse than none — though "redaction-gate-as-sole-egress" makes completeness enforceable in CI.
**Confidence:** 70%
**Complexity:** Low
**Status:** Unexplored

> **Telemetry decision (cross-#4/#7/#8):** same question ("how do we learn aggregate usage?"), three answers on a privacy spectrum — **local-only** (compute from #3's `outcome.json`, zero egress) → **#8 bucketed-DP** (anonymous aggregate, no third party) → **#7 BYO-endpoint** (richer data, into the *customer's own* stack with consent). Pick one as the default; #4 is the consent UX over whichever wins. #8 is the most brand-defensible; #7 the most operationally familiar.

## Rejection Summary

| # | Idea (frame) | Reason rejected / folded |
|---|---|---|
| 1 | "The recording IS the bug report" (assumption-breaking) | Too expensive/risky standalone — a whole recording is huge and max-PII; kept as a consideration in #2/#6 |
| 2 | Andon-cord operator-pull stop+capture (analogy) | Folded into #6 (capture volatile state at fault); standalone it's a recording-*control* feature, off-topic |
| 3 | Consent rides the per-app rail (assumption-breaking) | Folded into #4 as the consent-surface mechanism |
| 4 | OBD-II stable error codes (analogy) | Folded into #3 as the error-code sub-feature |
| 5 | Vitals heartbeat + syndromic-surveillance trend detection (analogy) | Folded into #6 |
| 6 | Consent ledger / clinical-trial tiered consent (leverage/analogy) | Folded into #4 as the backing store for tiered telemetry consent |
| 7 | Obsidian clipboard debug-info as a standalone idea (round-2) | Promoted instead into #2 as the v1 rung of the maturity ladder |
| 8 | "Add Sentry + PostHog" as a default cloud dependency (user-proposed) | Misaligned with the local-first/privacy wedge; reshaped into #7 (BYO-endpoint, dev-dogfood) and superseded as default by #8 (bucketed-DP) |
| 9 | ~20 duplicate doctor/bundle/excepthook/rotating-spine/telemetry variants across all 6 frames | Merged into the survivor each matches — strong convergence is signal, not extra ideas |

**Axis coverage:** all five axes have survivors (A:1 · B:2 · C:1 · D:4 · E:1) — no gaps. Axis D is intentionally dense because "how/whether/where telemetry lands" is the topic's hardest open question (#4 whether/what, #7 BYO-endpoint, #8 bucketed-DP, #9 auditable egress). The substrate (#1) deliberately spans all five as the shared foundation.

---

_Next step: `/ce-brainstorm` on a chosen idea (#8 and #2 are the most actionable seeds) to define it precisely enough for `ce-plan`._
