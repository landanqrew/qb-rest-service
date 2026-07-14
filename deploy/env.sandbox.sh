#!/usr/bin/env bash
# Shared sandbox env for both Cloud Run services. Source before either deploy
# script:
#
#     source deploy/env.sandbox.sh && deploy/deploy.sh          # -> qb-service-sandbox
#     source deploy/env.sandbox.sh && deploy/qb-admin.deploy.sh # -> qb-admin-sandbox
#
# With INTUIT_ENV=sandbox each deploy script defaults SERVICE to its own
# sandbox name, so a sandbox rollout never overwrites the prod service. An
# explicit SERVICE=... still overrides.

export GCP_PROJECT=martin-water-labs
export INTUIT_ENV=sandbox

# Distinct sandbox company realmId ("Sandbox Company_US_1"). NOTE: this is a
# different company from production (prod realm is 9130352324794416). The
# earlier assumption that both shared one realmId was wrong — the runtime
# client uses the token's realm_id, so a mismatch here is cosmetic, but keep
# it accurate.
export REALM_ID=9341456173900494

# Sandbox-specific Secret Manager secrets. These do NOT exist in
# martin-water-labs yet — create them before the first deploy:
#
#   gcloud secrets create mwl-qb-sandbox-client-id     --project=martin-water-labs
#   gcloud secrets create mwl-qb-sandbox-client-secret --project=martin-water-labs
#   gcloud secrets create mwl-qb-sandbox-tokens        --project=martin-water-labs
#
#   # Populate the two client secrets from the Intuit dashboard's Development
#   # Settings tab (echo -n to avoid a trailing newline in the stored value):
#   source .env.sandbox
#   echo -n $QBSVC_INTUIT_CLIENT_ID    | gcloud secrets versions add mwl-qb-sandbox-client-id     --data-file=- --project=martin-water-labs
#   echo -n $QBSVC_INTUIT_CLIENT_SECRET | gcloud secrets versions add mwl-qb-sandbox-client-secret --data-file=- --project=martin-water-labs
#
#   # Tokens blob starts empty; populated by the sandbox qb-admin OAuth flow
#   # (or by pushing the local tokens.sandbox.json we generate for the seed
#   # script — same JSON shape as SecretManagerTokenStore expects).
#   printf '' | gcloud secrets versions add mwl-qb-sandbox-tokens --data-file=- --project=martin-water-labs
#
# The runtime service account also needs roles/secretmanager.secretAccessor on
# each of these three (see deploy/iam-setup.md for the pattern used for prod).
export SECRET_CLIENT_ID=mwl-qb-sandbox-client-id
export SECRET_CLIENT_SECRET=mwl-qb-sandbox-client-secret
export SECRET_TOKENS=mwl-qb-sandbox-tokens

# --- qb-admin.deploy.sh only (ignored by deploy.sh) -------------------------
# Sandbox-facing consuming app URL. Use the sandbox/staging deployment of
# Sample Manager, or the same host with a query flag — whichever your app
# uses to indicate the sandbox connection.
export RETURN_URL=https://sample-manager--martin-water-labs.us-central1.hosted.app/settings/quickbooks
# Sandbox-specific admin launch secret. Does NOT exist in martin-water-labs
# yet — create it before deploying qb-admin-sandbox:
#
#   gcloud secrets create mwl-qb-sandbox-admin-launch --project=martin-water-labs
#   # Use a fresh random value; the consuming app's sandbox instance signs
#   # launch tokens with the same value.
#   openssl rand -hex 32 | tr -d '\n' | \
#     gcloud secrets versions add mwl-qb-sandbox-admin-launch --data-file=- --project=martin-water-labs
export SECRET_ADMIN_LAUNCH=mwl-qb-sandbox-admin-launch
