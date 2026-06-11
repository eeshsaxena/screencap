# ScreenCap Pilot Outreach Strategy — B2B SaaS Ops / CS-Ops

*Working draft. A few decisions are yours to make — flagged inline as **[your call]**.*

*Grounded in `STRATEGY.md`: the target is the internal-tool-heavy operator (CSMs, ops analysts, sales engineers) who reasons across 8–15 dashboards, and the strategic bet is the opt-in training corpus for computer-use agents.*

---

## 1. What this push is actually for

This is **not** a feedback beta. The goal is to sign a small set of pilots whose ops/CS-ops teams run ScreenCap daily, so you simultaneously (a) prove capture and redaction work in the real world and (b) start accumulating high-value computer-use traces for the agent-training corpus.

Every pilot is framed as a **value exchange**, never a favor. They run ScreenCap; you get the corpus as a byproduct.

**Suggested success definition for the first ~8 weeks [your call]:**
- 5–8 pilots signed
- Each pilot = 3–10 operator seats running ScreenCap a few hours/day for 3–4 weeks
- Enough consented traces from **one recurring multi-tool workflow** (see §3) to validate that the corpus is actually trainable

Lock these numbers before sending anything — they decide how many companies you need in the funnel.

---

## 2. The offer (the heart of the whole thing)

Do **not** sell "a screen recorder." Sell the outcome:

> "We turn what your ops team already does all day into (1) a map of where their time actually goes across your tools, and (2) a head start on automating the most repetitive cross-tool workflows — agents trained on your team's own real steps."

**What they get:** a workflow / time-allocation map; early access to draft automations for their top 1–2 recurring workflows; local-first, privacy-first handling (§4).

**What you get:** real-world validation of capture + redaction, and consented training traces.

This reframes the consent conversation entirely: it's about *their* automation roadmap, so they have a selfish reason to say yes.

---

## 3. What your first corpus should be — and your first agent

This is the decision that drives the targeting, taken straight from `STRATEGY.md`.

Your moat is **not** "automate one rote task." It's the **cross-tool, multi-dashboard workflow** that single-app tools and classic RPA can't see — the reasoning across Salesforce + the data warehouse + Looker + internal admin panels that is "currently lost." Your differentiator is rich signal (keyboard, window, network — not just screenshots).

**So: go deep, not broad — and make the depth one recurring, *multi-tool* workflow, not one narrow task.**

How that sorts the options we discussed:
- **Insurtech/fintech back-office** (claims, KYC) — *deprioritize.* Cleanest signal in the abstract, but it's 1–2 systems and rules-based — RPA territory. It under-uses your cross-tool edge and carries the heaviest consent burden. Off-strategy.
- **Marketplace / high-volume support** — *viable.* Resolving a ticket spans many tools, so it's real multi-dashboard work with fast volume. Cost: noisier, judgment-heavy traces.
- **B2B SaaS ops / CS-ops analyst** — *the bullseye.* This is the persona the strategy literally names, doing recurring cross-dashboard work (reporting, reconciliation, account hygiene) across the exact Salesforce/warehouse/Looker/admin stack. Multi-tool, recurring (so you get depth fast), judgment-light enough to automate, and low consent friction (internal business data, not regulated PII).

**Your first agent, concretely:** something that runs one recurring operator workflow end-to-end across several tools — e.g., *"every Monday, pull X from the warehouse and Looker, reconcile against Salesforce, flag the drifted accounts, update the dashboard."* That's almost a direct quote of the strategy's JTBD: "let an agent do the next round of the workflow for them."

*Honest caveat:* this makes the corpus bet and the consumer-wedge bet the same motion (good for focus), but it concentrates your eggs in the persona that's hardest to reach — busy internal ops people — so warm intros matter even more here.

---

## 4. Pre-empt the killer objection

The #1 reason an ops leader says no: *"you want my team to run an always-on screen recorder over company data."*

Put the answer **in the outreach itself**. ScreenCap's architecture is your best sales asset:
- **Local-first** — capture and processing happen on-device.
- **Capture-time redaction** — PII is filtered *before* anything hits disk (two-level, fail-closed).
- **Per-recording opt-in** — nothing leaves the device for the corpus without explicit consent on that recording.
- **Operators stay in control** — reversible until bytes actually leave the machine.

Have a **one-page privacy/consent explainer** ready before you send the first email. This is existential — write it first.

---

## 5. Apollo — targeting (title-first)

**The key principle: let the job title qualify the company, not the industry.** Industry can't tell you whether a company has a real ops function — plenty of "Computer Software" companies are tiny product-led shops with no ops team. But if someone holds the title "CS Ops Manager" or "Head of Revenue Operations," the company *by definition* has the operation you want. So lead with titles; treat industry as a loose net.

> Note on your free Apollo tier: Revenue, Funding, tech-stack, and Lookalike filters are locked. You don't need them for v1 — **industry + employee count + title** is enough, and title is doing most of the work anyway.

**Titles (the real qualifier) — search People for:**
- *Bullseye ops:* Revenue Operations / RevOps, CS Operations / Customer Success Operations, Business Operations Analyst, Sales Operations.
- *Champions who can authorize:* Head/Director/VP of Customer Success, Head of Customer Support, Director of Business Operations, COO (smaller cos).
- *Automation allies (often the warmest yes):* Head of AI / Automation, Director of Support Enablement.
- *Avoid leading with:* frontline agents (no authority), CISO/legal (they gate, not champion — bring in after a champion bites).

Target **2–3 contacts per company** (a bullseye ops person + a champion or ally).

**Industry (the loose net):**
- *Core:* Computer Software + Internet (i.e., B2B SaaS). Use these, **not** the broad "IT & Services" chip, which pulls in agencies and consultancies you don't want.
- *Worth widening into (tech-enabled, ops-heavy):* online marketplaces/platforms, fintech, insurtech, healthtech, edtech — tech-forward buyers with richer recurring ops work. Expect a longer privacy conversation in the regulated ones.

**Employee count:** 51–500 (use the full sidebar filter / custom range, not the quick chips that cap at 50). Big enough to have a real ops function; small enough that one champion can authorize a pilot without 6-month procurement.

**Geography:** where you can realistically onboard early pilots **[your call]**.

Target list size to start: **100–150 companies.**

---

## 6. The sequence (Apollo cadence)

Email-first, 3 steps over ~10 days. You're not at a volume that needs heavy multichannel, and cold-calling ops leaders is low-yield.

- **Email 1 (day 1)** — the workflow-map hook. Short. Problem → offer → soft 15-min ask.
- **Email 2 (day 4)** — credibility + the privacy reassurance (the §4 objection pre-empt). Optionally the "Rewind.ai shut down → local-first gap" framing.
- **Email 3 (day 9)** — give-to-get / breakup. Offer the workflow map as standalone value even with no pilot; make "not now" easy.
- **Optional:** one LinkedIn touch to the champion between emails 1 and 2.

**Deliverability guardrails (do not skip):**
- Send from a **separate domain**, not your primary.
- **Warm the domain** for ~2 weeks before sending.
- Keep volume low: **20–40 verified contacts/day.**
- Use Apollo's email verification to cut bounces.

---

## 7. Messaging — principles + sample

**Principles:** lead with their outcome, not your product category; one short specific ask; privacy in the body, not buried; name the *cross-tool* pain specifically (that's your differentiator); no "revolutionary AI platform" language.

**Sample Email 1**

> **Subject:** the cross-tool work your ops team can't see
>
> Hi {first} — quick one.
>
> Your ops/CS team spends the day stitching together {Salesforce, the warehouse, Looker, admin panels} — and that cross-tool work is invisible: no one can see which recurring sequences eat the most time, and it's the exact stuff that's hardest to automate.
>
> We built a local-first tool that maps it — and turns the most repetitive sequences into draft automations — without sending screens anywhere. It runs on-device with capture-time redaction, so company data never leaves the machine and your team stays in control.
>
> Worth 15 minutes to see if your team's a fit for an early pilot?
>
> — {name}

**Subject-line variants to A/B:** "where your ops team's hours actually go" · "automating your recurring cross-tool workflows" · "{Company}'s ops workflows → draft agents."

**Follow-up (Email 3) one-liner:** "Even if a pilot's not right now, I'm happy to share the workflow-map approach so your team can run it themselves — want me to send it over?"

---

## 8. Tracking

Log every contact and stage. You have **Notion** connected — a simple pipeline database works well (I can build it for you).

Track the funnel: contacted → replied → call booked → pilot agreed → installed → **traces flowing**. Review reply rate and objections weekly; iterate the copy every ~30–40 sends.

---

## 9. Risks & guardrails

- **Deliverability** — separate domain, warm-up, low volume, verified emails. Burn your main domain and the whole effort stalls.
- **Privacy objection is existential** — have the one-pager ready *before* sending.
- **Data quality** — confirm early that collected traces are actually usable for training before scaling pilots. Ties directly to the strategy's "% opting into corpus" metric and the "instrument the metrics" idea.
- **Hard-to-reach persona** — internal ops people are busy and skeptical; lean on warm intros wherever they exist (§3 caveat).
- **Don't overpromise the agents** — scope it as "drafts/prototypes for 1–2 workflows," not production automation.

---

## 10. First two weeks — checklist

**Week 1**
- [ ] Lock pilot definition + success metric (§1)
- [ ] Decide the one recurring multi-tool workflow you most want to own first (§3)
- [ ] Write the one-page privacy/consent explainer (§4)
- [ ] Set up + start warming a separate sending domain
- [ ] Build Apollo list: titles (§5) + industry net + 51–500 employees → 100–150 companies
- [ ] Enrich + verify a bullseye-ops contact + a champion/ally per company

**Week 2**
- [ ] Finalize the 3-email sequence in Apollo (§6–7)
- [ ] Build the Notion tracking pipeline (§8)
- [ ] Start sending 20–30/day
- [ ] Book first calls; prep a 15-min pilot pitch + short demo

---

## Decisions I need from you

1. **Pilot size & success metric** — the numbers in §1.
2. **The first workflow** — which recurring multi-tool ops workflow do you most want your first agent to own (§3)? If you're unsure, we pick it *from* the first few pilots' time-maps.
3. **Geography** — where can you realistically onboard early pilots?
4. **The "agents" deliverable** — how real is it today? It shapes how boldly you can pitch §2.
