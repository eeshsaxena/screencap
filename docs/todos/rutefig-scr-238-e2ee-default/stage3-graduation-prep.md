# SCR-238 Stage-3 prep — KD5 re-confirmation + graduation-criterion refinement

**Ticket:** SCR-238 (Make cloud E2EE the default — Stage 3, units U9–U10 of `docs/plans/2026-07-11-002-feat-scr-220-e2ee-shared-copies-plan.md`)
**Date:** 2026-07-12
**Scope of this note:** the two pieces of SCR-238 that are *unblocked* today — (A) re-confirm the KD5 server-side ciphertext no-op posture, and (B) refine the KTD-7 graduation criterion with a real observation channel + minimum-usage floor, as the ticket itself asks ("fold that into the criterion before counting the clock").

**This note does NOT flip the default.** U9 (`cloud_e2ee_enabled` default flip) and U10 (claim unlock) remain gated by KTD-7. `cloud_e2ee_enabled` still defaults `False` (`src/screencap/config.py:380`).

---

## Gate status snapshot (why the flip is not happening)

KTD-7 requires **all three** before U9 starts. As of 2026-07-12:

| KTD-7 condition | Status | Evidence |
|---|---|---|
| 1. Stage 2 (SCR-253) shipped | ✅ **Shipped** | PR #377 merged to `main` 2026-07-12 10:11 UTC (U7 `17fafcc4`, U8 `7a0a9018`/`006bb8af`). `keychain_group.py` has `synchronizable` wired through `_sync_pair`. |
| 2. Second-Mac decrypt verified on real hardware | ⚠️ **Accepted on trust for now** (owner decision) | No two-Mac hardware available; not pre-verified. Real-hardware evidence is deferred to the soak channel's recurring second-Mac canary (§ Part B). Safe **only because the soak remains the net** — a broken sync path is caught before it can lose data. |
| 3. 4-week soak, zero crypto-path data-loss incidents | ❌ **Clock not started** | Soak channel does not exist yet (this note defines it); the clock can only start once Stage 2 is soaking with an active channel. |

---

## Part A — KD5 re-confirmation: server-side ciphertext no-op

**Decision under review (plan KD5 / R13):** the website's direct-from-GCS player and the Eventarc `process-recording` enrichment service read plaintext by construction; under E2EE they apply only to the future server-readable corpus lane, and their behavior for encrypted objects is a *documented no-op*. This was SCR-238's original hard prerequisite. The ticket says: "confirm that decision is still current before flipping."

### Finding: CONFIRMED — the processor no-ops on ciphertext, by construction.

`scripts/process-recording/main.py` has **zero encryption awareness** — a full grep for `encrypt|cipher|magic|decrypt|e2ee|plaintext` returns nothing. The no-op is emergent from the read path:

1. The handler is triggered by `recording_complete.json` (or legacy `recording.db`) landing under `recordings/<name>/` (`main.py:1582-1597`).
2. It lists `chunk_NNNN_manifest.json` blobs and parses each with `json.loads` (`main.py:293-302`, `_load_manifest`). **A ciphertext manifest fails `json.loads` → returns `None` → the manifest is dropped.**
3. If every manifest is ciphertext, the filtered `manifests` list is empty and the handler exits at `if not manifests: log.error("All manifests failed to load"); return` (`main.py:1719-1721`) — **no `sessions/<name>/` output is ever written.** Nothing is decrypted; no plaintext is derived from ciphertext.
4. The website reads processed `sessions/<name>/` output (`timeline.json`, `sessions/_index.json`, `main.py:1514-1575`, `1830`). Because that output is never produced for an encrypted recording, the recording simply never surfaces on the website — the cleanest possible no-op.

**Net:** no code path in this repo decrypts or leaks plaintext from an encrypted object. KD5 is still current in code.

### Caveat to record (do not silently rely on it)

The no-op is **emergent (JSON-parse-gated), not an asserted guard.** There is no explicit "is this ciphertext? then skip" check and **no test pins the behavior.** Two ways it could silently drift:

- A future change that makes `_load_manifest` more lenient (e.g., tolerant parsing, a binary manifest format) could start emitting partial output from ciphertext.
- If a recording ever uploaded a *plaintext* manifest alongside *ciphertext* chunks (a partial-encryption bug), the processor would happily process the plaintext manifest and try to `ffmpeg`-read ciphertext chunks — producing empty/garbage tasks rather than a clean skip.

**Recommendation (cheap, not blocking the flip):** before Stage 3 ships, either (a) add an explicit early-return in the handler when a manifest carries the `cloud_crypto.MAGIC` (`b"SCRE2E"`, `src/screencap/cloud_crypto.py:103`) prefix, plus a one-line test, or (b) at minimum document in `SECURITY.md` that the no-op is parse-gated and the corpus lane is the *only* intended server-readable path — so a future `_load_manifest` refactor knows it is load-bearing. This is the same "documented not fixed" posture KD5 already takes; the ask is to make it legible at the code site.

### Out of this checkout: the website player

The website lives in the sibling repo `screencap-website` (not present here). The structural argument above (no `sessions/<name>/` produced ⇒ nothing to render) means the player has nothing to surface for encrypted recordings, but **that repo should be independently confirmed** — specifically that it never attempts to stream raw `recordings/<name>/chunk_*.mp4` objects directly (which would be ciphertext and fail to decode). Tracked as an open item below.

---

## Part B — Refined graduation criterion (KTD-7 condition 3)

The code review flagged that the soak "needs an actual observation channel + a minimum-usage floor (not just elapsed calendar time with few users)." This section makes that concrete, grounded in what the product can actually observe today.

### Why elapsed calendar time observes nothing

The soak is meant to catch **crypto-path data-loss incidents**. The product's ability to *see* one is currently near-zero:

- **No product telemetry.** No PostHog / Amplitude / Mixpanel / Segment client integration exists (every match in `src/` and `macos/` was a docstring or comment). The `emit_event` calls in `upload.py` feed the daemon's **local** SSE bus (`/v0/events`) for CLI/app progress — they are not sent anywhere.
- **No crash/error reporting** (no Sentry/Crashlytics/Bugsnag).
- **No in-app cloud download/playback.** Per U8, the app has no cloud-decrypt surface today — `download.py`'s decrypt path (`download.py:244-271`) is only hit by the `screencap download` CLI. So in normal use **the decrypt path is never exercised**, which means the "undecryptable with key present" failure mode has no natural trigger.
- **Fail-closed is not an incident** (KTD-7). Upload refusals when no key is present (`upload.py` fail-closed key resolution), `.scrub_failed` sidecars, and key-unavailable download markers are *expected* behavior, not data loss. They must be filtered out, or they either abort the soak spuriously or drown a real signal.

Consequence: "4 weeks with few users and no telemetry" could pass with a systematic crypto bug fully intact, simply because nobody exercised the read path and nothing reported the failure. The channel has to be **actively constructed and exercised**, not passively watched.

### Precise incident definition (restated, with the retention nuance)

An **incident** is exactly one of:

1. A **frozen-on recording whose local plaintext was evicted** AND whose cloud copy **will not decrypt with the key present** (permanently unreadable customer data — the "dead Mac" scenario made real).
2. Any **decrypt failure of a frozen-on object** while `e2ee status` reports the key present — e.g. a `cloud_crypto.KeyIdMismatch` (`cloud_crypto.py:137`, "object's header key id does not match the available cloud key") or a GCM auth failure on an object whose `key_id` matches the resolved key.

A **non-incident** is any fail-closed event: upload refused for want of a key, `.scrub_failed`, or a key-unavailable state on a Mac that never had the key (the R7/U8 UX path).

**Retention nuance that makes incident (1) real even today:** the eviction floor (`src/screencap/retention.py:11-20`) re-confirms an object **exists** remotely (`remote_exists` → `_chunk_confirmed_remote`) before deleting local plaintext. Under E2EE, *exists ≠ decryptable*. A frozen-on object can satisfy the retention floor (present in GCS) and still be incident-class-1 unreadable (bad key_id, corrupted frames, lost KEK). The observation channel must therefore verify **decryptability**, not just remote existence.

### The observation channel (what must exist before the clock counts)

Because the server sees nothing by design and there is no telemetry, the channel is a deliberately-exercised, locally-instrumented loop across the soak cohort:

1. **Round-trip decrypt canary (primary).** On each participating opted-in Mac, per soak period: take a known frozen-on recording → confirm GCS holds ciphertext (`cloud_crypto.is_encrypted_prefix` on the stored object, `cloud_crypto.py:404`) → `screencap download` it back → decrypt (`download.py:261-271`) → checksum against the local original. A checksum mismatch, or a decrypt failure while the key is present, is an incident-class-2 signal. This is the only thing that exercises the read path at all.
2. **Second-Mac decrypt canary (once Stage 2 ships).** The same round-trip performed from a *second* Mac (same Apple ID, iCloud Keychain on). This subsumes KTD-7 condition 2 — but **recurring within the soak**, not a one-time check, so a sync regression during the window is caught. Must pass ≥ once per soak period per participating multi-Mac user.
3. **Eviction-after-remote-confirm interlock (tripwire).** Instrument the retention path so that any eviction of a frozen-on recording's local plaintext during the soak is preceded by a *successful fresh remote decrypt* (not just remote existence). An eviction that proceeds on existence-only, followed by a failed decrypt, is incident-class-1 — the exact failure the soak exists to catch.
4. **Fail-closed discriminator.** Every anomaly is classified against the incident definition above before it counts. Fail-closed noise is logged and excluded.

**Hard prerequisite (this is what "before counting the clock" means):** there is **no central channel to aggregate any of the above** across users. So one of the following must exist *before* the soak clock starts, or there is literally nothing to count:
- (a) a small hand-instrumented cohort whose canary jobs report into a shared location the team watches, or
- (b) a minimal opt-in soak-metrics reporter (counts + incident flags only, no recording content) built for the beta.

Option (b) is the more defensible one and is arguably a small unit of its own. Either way, **the channel is a prerequisite, not a byproduct of waiting.**

### Minimum-usage floor (proposed — team to ratify the numbers)

The window is meaningful only if the crypto path was exercised at volume, not merely that 28 days elapsed. Proposed floors (numbers are a product call given the small beta population — presented as a starting point, not a decided value):

- **≥ 5 distinct opted-in Macs** actively uploading frozen-on recordings (enough that a systematic bug shows up across configs).
- **≥ 100 frozen-on recordings** uploaded and confirmed ciphertext-at-rest across the cohort (a volume floor for the write path).
- **≥ 20 successful round-trip decrypts** (encrypt → GCS → download → decrypt → checksum-match), **including ≥ 1 second-Mac decrypt per participating multi-Mac user** (proves the read path, not just the write path).
- **≥ 1 real eviction-after-remote-confirm cycle** observed end-to-end (a frozen-on recording whose local plaintext was evicted and later re-decrypted from cloud) — proves the actual data-loss scenario is survivable.
- **Zero incidents** (as defined) across all of the above, over a **continuous** window (plan: 4 weeks). **The clock resets on any incident.**

### Proposed replacement wording for KTD-7 condition 3

> **3.** A defined opt-in soak with an active observation channel and a minimum-usage floor, not calendar time alone. The channel (round-trip decrypt canary + second-Mac canary + eviction-after-decrypt interlock, with fail-closed events discriminated out) must be in place before the clock starts. The floor requires ≥ 5 opted-in Macs, ≥ 100 frozen-on recordings confirmed ciphertext-at-rest, ≥ 20 successful round-trip decrypts (incl. ≥ 1 second-Mac decrypt per multi-Mac user), and ≥ 1 observed eviction-after-remote-decrypt cycle. The window is a continuous 4 weeks with **zero** crypto-path data-loss incidents (fail-closed events are not incidents; an incident is a frozen-on object that is undecryptable with the key present, or lost plaintext whose cloud copy will not decrypt). Any incident resets the clock.

---

## Open items (before Stage 3 can legitimately start)

- [ ] **Ratify the floor numbers** (above) with the team, or set the beta-appropriate values.
- [ ] **Decide + build the aggregation channel** — hand-instrumented cohort vs. minimal soak-metrics reporter (option (b)). This is the true blocker on "starting the clock."
- [ ] **Confirm the `screencap-website` player** never streams raw `recordings/<name>/chunk_*.mp4` (sibling repo, out of this checkout).
- [ ] **(Cheap hardening, optional pre-flip):** make the processor's ciphertext no-op explicit (MAGIC-prefix early-return + one test) or document it as parse-gated and load-bearing in `SECURITY.md`.
- [x] Ship SCR-253 (Stage 2) — done, PR #377 merged 2026-07-12. *(KTD-7 cond. 1)*
- [~] **Second-Mac decrypt on hardware** *(KTD-7 cond. 2)* — **accepted on trust for now** (owner decision, no two-Mac hardware). Real-hardware verification is deferred to the soak channel's recurring second-Mac canary; the residual risk is held by the soak-as-net. Optional de-risk before opening the beta: a macOS VM or rented/cloud Mac signed into the same Apple ID.

**Only when all of KTD-7 holds does U9 (the one-line `config.py` default flip) + U10 (claim unlock) begin.**
