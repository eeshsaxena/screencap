#!/usr/bin/env bash
#
# Deploy the billing + signer Cloud Functions with live secrets injected from
# GCP Secret Manager (plan U3 / KTD-7).
#
# There is no deploy automation otherwise — the commands live only in the
# billing.py / main.py docstrings, and the --set-secrets pattern is demonstrated
# only in feedback.py. This wrapper turns that manual, error-prone translation
# into one reviewed, repeatable path:
#   - live secrets (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET) come from Secret
#     Manager via --set-secrets, NEVER inline (a raw secret in the env is
#     refused up front);
#   - the billing functions get GOOGLE_FUNCTION_SOURCE=billing.py so the buildpack
#     doesn't default to main.py;
#   - the signer is deployed dark (STRIPE_PAYWALL_ENFORCE left unset = off);
#   - it is fail-fast: on any deploy failure it halts and reports which functions
#     already landed (no automatic rollback), and it deploys the entitlement
#     webhook + reconcile BEFORE the checkout path so a partial failure can never
#     leave a live charge path without its grantor.
#
# Usage:
#   # Preview the exact gcloud commands (secret *references*, never values):
#   STRIPE_PRICE_ID_LOCAL=price_L STRIPE_PRICE_ID_CLOUD=price_C \
#     scripts/cloud-function/deploy_billing.sh --dry-run
#
#   # Real deploy (secrets already stored in Secret Manager):
#   STRIPE_PRICE_ID_LOCAL=price_L STRIPE_PRICE_ID_CLOUD=price_C \
#   STRIPE_PORTAL_CONFIGURATION_ID=bpc_... \
#     scripts/cloud-function/deploy_billing.sh
#
# Config (env vars, with defaults):
#   PROJECT (proteus-photos), REGION (southamerica-east1), SOURCE (scripts/cloud-function/)
#   BILLING_SA, SIGNER_SA — service accounts
#   SECRET_STRIPE_SECRET_KEY (stripe-secret-key), SECRET_STRIPE_WEBHOOK_SECRET
#     (stripe-webhook-secret) — Secret Manager secret NAMES (not values)
#   STRIPE_PRICE_ID_LOCAL, STRIPE_PRICE_ID_CLOUD — required
#   STRIPE_PRICE_ID — legacy $5 price (optional; grandfather map)
#   TRIAL_PERIOD_DAYS (7), STRIPE_PORTAL_RETURN_URL (https://screencap.sh/account)
#   STRIPE_PORTAL_CONFIGURATION_ID — bpc_... (required for the live portal)
#   SCREENCAP_BUCKET (screencap-recordings) — signer's target bucket

set -euo pipefail

DRY_RUN=0
case "${1:-}" in
  --dry-run | --print) DRY_RUN=1 ;;
  "") ;;
  *)
    echo "Unknown argument: $1 (expected --dry-run or no argument)" >&2
    exit 2
    ;;
esac

PROJECT="${PROJECT:-proteus-photos}"
REGION="${REGION:-southamerica-east1}"
SOURCE="${SOURCE:-scripts/cloud-function/}"
BILLING_SA="${BILLING_SA:-screencap-billing@${PROJECT}.iam.gserviceaccount.com}"
SIGNER_SA="${SIGNER_SA:-screencap-signer@${PROJECT}.iam.gserviceaccount.com}"
SECRET_STRIPE_SECRET_KEY="${SECRET_STRIPE_SECRET_KEY:-stripe-secret-key}"
SECRET_STRIPE_WEBHOOK_SECRET="${SECRET_STRIPE_WEBHOOK_SECRET:-stripe-webhook-secret}"

TRIAL_PERIOD_DAYS="${TRIAL_PERIOD_DAYS:-7}"
STRIPE_PORTAL_RETURN_URL="${STRIPE_PORTAL_RETURN_URL:-https://screencap.sh/account}"
SCREENCAP_BUCKET="${SCREENCAP_BUCKET:-screencap-recordings}"

# Refuse raw secrets in the environment — they must come from Secret Manager.
# Covers BOTH the value-form vars and the SECRET_* name vars: the latter are what
# get interpolated into --set-secrets and echoed by --dry-run, so a raw key
# mistakenly placed there (instead of a Secret Manager NAME) must also be refused.
for var in STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET SECRET_STRIPE_SECRET_KEY SECRET_STRIPE_WEBHOOK_SECRET; do
  val="${!var:-}"
  case "$val" in
    sk_* | rk_* | whsec_*)
      echo "ERROR: $var is set to a raw secret value." >&2
      echo "Live secrets must come from Secret Manager via --set-secrets, never" >&2
      echo "inline. Unset $var and set SECRET_${var} to the Secret Manager secret" >&2
      echo "NAME instead (default: stripe-secret-key / stripe-webhook-secret)." >&2
      exit 1
      ;;
  esac
done

# Required non-secret config. Missing prices are fatal on a real deploy and a
# warning in --dry-run (so the operator can preview the command shape first).
missing=()
[ -n "${STRIPE_PRICE_ID_LOCAL:-}" ] || missing+=("STRIPE_PRICE_ID_LOCAL")
[ -n "${STRIPE_PRICE_ID_CLOUD:-}" ] || missing+=("STRIPE_PRICE_ID_CLOUD")
if [ "${#missing[@]}" -gt 0 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "WARNING (dry-run): unset required config: ${missing[*]}" >&2
  else
    echo "ERROR: unset required config: ${missing[*]}" >&2
    exit 1
  fi
fi
if [ -z "${STRIPE_PORTAL_CONFIGURATION_ID:-}" ]; then
  echo "WARNING: STRIPE_PORTAL_CONFIGURATION_ID is unset — the live customer" >&2
  echo "portal fails closed without it (billing.py U7). Set it before go-live." >&2
fi

# Comma-separated env strings. Empty values are harmless: billing.py skips an
# empty price in its price->tier map, and treats an empty portal config as unset.
PRICE_ENV="STRIPE_PRICE_ID_LOCAL=${STRIPE_PRICE_ID_LOCAL:-},STRIPE_PRICE_ID_CLOUD=${STRIPE_PRICE_ID_CLOUD:-},STRIPE_PRICE_ID=${STRIPE_PRICE_ID:-}"

SUCCEEDED=()

deploy() {
  local name="$1"
  shift
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'gcloud functions deploy %s' "$name"
    printf ' %q' "$@"
    printf '\n\n'
    return 0
  fi
  echo ">>> Deploying ${name} ..."
  if gcloud functions deploy "$name" "$@"; then
    SUCCEEDED+=("$name")
  else
    echo "ERROR: deploy failed for ${name}." >&2
    echo "Functions deployed this run: ${SUCCEEDED[*]:-none}." >&2
    echo "Halting — resolve and re-run so all functions land on the same mode." >&2
    exit 1
  fi
}

# Shared billing-function flags.
billing_common=(
  --project "$PROJECT" --gen2 --runtime python312
  --trigger-http --allow-unauthenticated
  --region "$REGION" --source "$SOURCE"
  --service-account "$BILLING_SA"
  --set-build-env-vars GOOGLE_FUNCTION_SOURCE=billing.py
)

# Deploy the entitlement GRANTORS (webhook, reconcile) before the checkout path,
# so a partial deploy failure can never leave a live charge path (checkout)
# running without its webhook to grant entitlement.

# 1. stripe-webhook — signature-verified; needs BOTH secrets + the price->tier map.
deploy stripe-webhook "${billing_common[@]}" \
  --entry-point stripe_webhook \
  --set-secrets "STRIPE_SECRET_KEY=${SECRET_STRIPE_SECRET_KEY}:latest,STRIPE_WEBHOOK_SECRET=${SECRET_STRIPE_WEBHOOK_SECRET}:latest" \
  --set-env-vars "SCREENCAP_PROJECT_ID=${PROJECT},${PRICE_ENV}"

# 2. reconcile-entitlement — Firebase-gated grant-only self-heal.
deploy reconcile-entitlement "${billing_common[@]}" \
  --entry-point reconcile_entitlement \
  --set-secrets "STRIPE_SECRET_KEY=${SECRET_STRIPE_SECRET_KEY}:latest" \
  --set-env-vars "SCREENCAP_PROJECT_ID=${PROJECT}"

# 3. create-checkout-session — Firebase-gated; needs the secret key + prices.
deploy create-checkout-session "${billing_common[@]}" \
  --entry-point create_checkout_session \
  --set-secrets "STRIPE_SECRET_KEY=${SECRET_STRIPE_SECRET_KEY}:latest" \
  --set-env-vars "SCREENCAP_PROJECT_ID=${PROJECT},${PRICE_ENV},TRIAL_PERIOD_DAYS=${TRIAL_PERIOD_DAYS}"

# 4. stripe-portal-session — Firebase-gated; needs the price map + portal config.
deploy stripe-portal-session "${billing_common[@]}" \
  --entry-point create_portal_session \
  --set-secrets "STRIPE_SECRET_KEY=${SECRET_STRIPE_SECRET_KEY}:latest" \
  --set-env-vars "SCREENCAP_PROJECT_ID=${PROJECT},${PRICE_ENV},STRIPE_PORTAL_RETURN_URL=${STRIPE_PORTAL_RETURN_URL},STRIPE_PORTAL_CONFIGURATION_ID=${STRIPE_PORTAL_CONFIGURATION_ID:-}"

# 5. get-upload-urls (signer) — main.py default source, no Stripe secret,
#    STRIPE_PAYWALL_ENFORCE left UNSET (dark) so it deploys enforce-off.
deploy get-upload-urls \
  --project "$PROJECT" --gen2 --runtime python312 \
  --trigger-http --allow-unauthenticated \
  --region "$REGION" --source "$SOURCE" \
  --entry-point get_upload_urls \
  --service-account "$SIGNER_SA" \
  --set-env-vars "SCREENCAP_BUCKET=${SCREENCAP_BUCKET},SCREENCAP_PROJECT_ID=${PROJECT}"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] Printed 5 deploy commands; no functions were deployed."
else
  echo "Deployed: ${SUCCEEDED[*]}."
  echo "Next: run verify_live_readiness.py, then the U5 synthetic-event check."
fi
