#!/usr/bin/env bash
# Shared prod env for both Cloud Run services. Source before either deploy
# script:
#
#     source deploy/env.production.sh && deploy/deploy.sh          # data API
#     source deploy/env.production.sh && deploy/qb-admin.deploy.sh # admin OAuth
#
# SERVICE is intentionally NOT exported here — each deploy script defaults to
# the correct service name for its own surface (qb-service / qb-admin).

export GCP_PROJECT=martin-water-labs
export INTUIT_ENV=production
export REALM_ID=9130352324794416

# Secret Manager secret names in ${GCP_PROJECT}. These already exist in the
# qrewtech project; when standing up martin-water-labs they must be created
# (see deploy/env.sandbox.sh for the gcloud commands — same shape).
export SECRET_CLIENT_ID=mwl-qb-client-id
export SECRET_CLIENT_SECRET=mwl-qb-client-secret
export SECRET_TOKENS=mwl-qb-tokens

# --- qb-admin.deploy.sh only (ignored by deploy.sh) -------------------------
# Where the browser returns after a successful Connect-QuickBooks flow — the
# prod URL of the consuming app (Sample Manager) that hosts the button.
export RETURN_URL=https://sample-manager--martin-water-labs.us-central1.hosted.app/settings/quickbooks
export SECRET_ADMIN_LAUNCH=mwl-qb-admin-launch
