#!/usr/bin/env bash
# Shared sandbox env for both Cloud Run services. Source before either deploy
# script; sandbox uses distinct service names so both env deploys can coexist:
#
#     source deploy/env.sandbox.sh && SERVICE=qb-service-sandbox deploy/deploy.sh
#     source deploy/env.sandbox.sh && SERVICE=qb-admin-sandbox   deploy/qb-admin.deploy.sh
#
# SERVICE is set inline (not exported here) because the two deploy scripts
# want different names and we don't want the same env file to force one.

export GCP_PROJECT=martin-water-labs
export INTUIT_ENV=sandbox

# Same realmId as prod — only the Intuit app's client_id/secret differ
# between sandbox and production.
export REALM_ID=9130352324794416

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

