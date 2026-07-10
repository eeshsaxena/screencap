# Paid-Only Launch Pricing — Cutover Runbook (U13)

Operational cutover for the two-tier (Local Pro / Cloud) paid-only launch.
Plan: `docs/plans/2026-07-10-001-feat-paid-only-launch-pricing-plan.md`.
Extends the shipped $5 Personal-cloud paywall (PR #348). GCP project
`proteus-photos`, region `southamerica-east1`.

Everything lands **dark** behind three default-off flags and is flipped only at
cutover. Nothing here changes behavior until a flag is flipped.

## The three flags (decoupled by design)

| Flag | Where | Governs | Default |
|---|---|---|---|
| `SCREENCAP_STRIPE_PAYWALL` | client (app/CLI, `config.py`) | pricing UI + checkout routing + soft gate | off |
| `STRIPE_PAYWALL_ENFORCE` | signer Cloud Function env | cloud upload **hard** gate (unchanged this launch) | off |
| `SCREENCAP_LOCAL_PAYWALL_ENFORCE` | client/daemon (`config.py`) | local recording-start + recall gates | off |

Flip the two client flags (`SCREENCAP_STRIPE_PAYWALL`, `SCREENCAP_LOCAL_PAYWALL_ENFORCE`)
**only once the paywall-capable DMG is the minimum shipped app version** — older
clients have no in-app way to pay and would be hard-gated (KTD-8, plan Stop conditions).

## Cutover steps

1. **Provision Stripe.** Create two **recurring** prices — Local Pro and Cloud —
   plus (optional) a launch promo code and a webhook endpoint pointing at the
   deployed `stripe-webhook`. Keep the legacy $5 Price id: it is grandfather-mapped
   to `tier=cloud` (KTD-2, R11), not retired. See `scripts/cloud-function/.env.example`.

2. **Deploy the functions** (billing + signer) with the two-tier env. Billing
   entry points need `GOOGLE_FUNCTION_SOURCE=billing.py` and the price envs
   `STRIPE_PRICE_ID_LOCAL`, `STRIPE_PRICE_ID_CLOUD`, `TRIAL_PERIOD_DAYS`, and (webhook)
   the legacy `STRIPE_PRICE_ID`. Full `gcloud functions deploy` commands are in the
   `billing.py` module docstring. Deploy the signer with `STRIPE_PAYWALL_ENFORCE`
   **off** first to validate the loop dark. Live keys via GCP Secret Manager
   (`--set-secrets`), never a file.

3. **Seed comps** — before enforcing anything, grant internal/demo accounts:
   `python scripts/set_entitlement.py --email <internal@…> --tier cloud`
   (repeat per internal account). This is the launch-day seatbelt (Stripe-independent).

4. **Grandfather existing $5 subscribers** — idempotent, re-runnable:
   `python scripts/set_entitlement.py --backfill-grandfathered`
   sets `tier=cloud` for every account currently carrying `subscribed=true`. Safe to
   re-run if it partially fails. Their Stripe subscription keeps its $5 price; the
   webhook's legacy-price map keeps their ongoing events resolving `tier=cloud`.

5. **Verify the full loop on the dev function** (test-mode) before flipping any flag:
   - Card-required trial checkout on **both** tiers → `trialing` entitles → record +
     recall work.
   - Simulate lapse → recording-start and the five recall verbs gated; browse
     (`recording.list`/`timeline.day`/`tasks.list`) + export + cloud-download stay open;
     cloud upload returns 402.
   - A `tier=local` token is refused a cloud upload URL; a `tier=cloud` token uploads.
   - A grandfathered $5 account still records, recalls, and uploads.
   - Pay-then-immediately-record is un-gated without the ~1h token wait (the app calls
     `POST /v0/entitlement.refresh`, which force-re-mints the daemon token and rewrites
     the lease).
   - An offline paying account keeps recording within the ~72h lease window; a
     non-payer who stays offline past lease expiry is gated (perpetual-offline closed).

6. **Flip flags at cutover** — set `STRIPE_PAYWALL_ENFORCE` on (signer), and the two
   client flags on, **only after** comps + grandfather are seeded and the paywall DMG
   is the minimum shipped version.

## Rollback

All three flags are default-off and independent, so rollback is a dark flip: turn
`SCREENCAP_LOCAL_PAYWALL_ENFORCE` and `SCREENCAP_STRIPE_PAYWALL` off (client) and/or
`STRIPE_PAYWALL_ENFORCE` off (signer). No data is deleted on lapse or rollback;
grandfather/comp claims persist.

## Notes

- The signer's cloud gate code is **unchanged** this launch (U5 is a regression guard).
- The local gates are **soft** (client-side, best-effort) — see `SECURITY.md` and
  plan KTD-4 for the trust boundary; the ~72h lease bounds both offline-grace and
  lapse-enforcement latency.
