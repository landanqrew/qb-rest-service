#!/usr/bin/env bash
# Build and deploy qb-admin — the PUBLIC, browser-safe OAuth bootstrap service
# (issue #51).
#
# qb-admin runs the SAME image as qb-service but is deployed as a distinct,
# public Cloud Run service that hosts ONLY the /admin/oauth/* bootstrap surface.
# It exists so a consuming app can offer a real, in-browser
# "Connect QuickBooks" button: a plain browser cannot attach a Cloud Run identity
# token, so the IAM-locked qb-service can't host a user-facing connect flow.
#
# The split (scope doc §7 "second Cloud Run service", issue #51):
#   qb-service : --no-allow-unauthenticated, data /v1/* only (admin routes OFF).
#   qb-admin   : --allow-unauthenticated,   /admin/oauth/* only (data routes OFF).
#
# qb-admin is public at the edge but NOT open: /admin/oauth/{start,disconnect}
# require a signed *launch token* (QBSVC_ADMIN_LAUNCH_SECRET) that only the
# consuming app can mint. Both services share the same token Secret Manager
# secret, so a connect/re-auth done here is immediately visible to qb-service.
#
# Required env vars (export before running, pass on the command line, or set
# ENV_FILE=.env.production / ENV_FILE=.env.sandbox so this script loads them):
#   GCP_PROJECT   GCP project ID hosting Cloud Run + Secret Manager
#   REALM_ID      Intuit realm (company) ID
#   RETURN_URL    Where the browser is sent after a successful connect — the
#                   consuming app's page (e.g. https://your-app.example.com/
#                   settings/integrations). Sets QBSVC_ADMIN_RETURN_URL.
#   SECRET_CLIENT_ID
#                 Secret Manager secret containing Intuit OAuth client ID
#   SECRET_CLIENT_SECRET
#                 Secret Manager secret containing Intuit OAuth client secret
#   SECRET_TOKENS
#                 Secret Manager secret containing rotated token JSON
#   SECRET_ADMIN_LAUNCH
#                 Secret Manager secret containing the launch-token signing key
# Optional (with defaults):
#   REGION        Cloud Run region (default: us-central1)
#   SERVICE       Service name (default: qb-admin)
#   AR_REPO       Artifact Registry repo (default: qb-service — same image)
#   RUNTIME_SA    Runtime SA (default: qb-admin-runtime@${GCP_PROJECT}.iam.gserviceaccount.com)
#   INTUIT_ENV    "production" or "sandbox" (default: production)
#
# Prerequisites (see deploy/qb-admin-setup.md and deploy/iam-setup.md):
#   - Artifact Registry repo ${AR_REPO} exists in ${REGION}.
#   - A dedicated runtime SA (qb-admin-runtime) with
#       roles/secretmanager.secretAccessor on ${SECRET_CLIENT_ID},
#       ${SECRET_CLIENT_SECRET}, ${SECRET_TOKENS}, and ${SECRET_ADMIN_LAUNCH}.
#   - Secret ${SECRET_ADMIN_LAUNCH} holds the shared launch-token secret; the
#     same value is configured in the consuming app so it can mint launch tokens.
#   - The Intuit console has this service's /admin/oauth/callback registered as a
#     redirect URI (production keys require a public https URL — this is it).
#
# NOTE: this service deliberately does NOT set QBSVC_ADMIN_ALLOWLIST. The IAM
# allowlist gate is meaningless for a public service with no inbound JWT; the
# launch-token gate is what protects /admin/oauth/*.

set -euo pipefail

if [[ -n "${ENV_FILE:-}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

GCP_PROJECT="${GCP_PROJECT:?GCP_PROJECT is required}"
REALM_ID="${REALM_ID:?REALM_ID is required}"
RETURN_URL="${RETURN_URL:?RETURN_URL is required (consuming app URL to return to after connect)}"
REGION="${REGION:-us-central1}"
INTUIT_ENV="${INTUIT_ENV:-production}"

if [[ ! "${INTUIT_ENV}" =~ ^(production|sandbox)$ ]]; then
  echo "ERROR: INTUIT_ENV must be 'production' or 'sandbox', got: ${INTUIT_ENV}" >&2
  exit 1
fi

# Sandbox deploys to its own service so a sandbox rollout never overwrites the
# production revision. An explicit SERVICE always overrides this default.
if [[ "${INTUIT_ENV}" == "sandbox" ]]; then
  SERVICE="${SERVICE:-qb-admin-sandbox}"
else
  SERVICE="${SERVICE:-qb-admin}"
fi
AR_REPO="${AR_REPO:-qb-service}"
RUNTIME_SA="${RUNTIME_SA:-qb-admin-runtime@${GCP_PROJECT}.iam.gserviceaccount.com}"
SECRET_CLIENT_ID="${SECRET_CLIENT_ID:?SECRET_CLIENT_ID is required}"
SECRET_CLIENT_SECRET="${SECRET_CLIENT_SECRET:?SECRET_CLIENT_SECRET is required}"
SECRET_TOKENS="${SECRET_TOKENS:?SECRET_TOKENS is required}"
SECRET_ADMIN_LAUNCH="${SECRET_ADMIN_LAUNCH:?SECRET_ADMIN_LAUNCH is required}"

GIT_SHA="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/qb-service:${GIT_SHA}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Building image ${IMAGE}"
gcloud builds submit "${REPO_ROOT}" \
  --project="${GCP_PROJECT}" \
  --config="${REPO_ROOT}/cloudbuild.yaml" \
  --substitutions="_IMAGE=${IMAGE}"

# Reuse the existing service URL on redeploys so the Intuit-registered redirect
# URI stays byte-identical across revisions.
EXISTING_URL="$(gcloud run services describe "${SERVICE}" \
  --project="${GCP_PROJECT}" --region="${REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"

# Data routes OFF: this public surface hosts admin OAuth bootstrap only.
# `^|^` sets `|` as the delimiter so URL values containing commas pass through.
ENV_PAIRS="^|^QBSVC_ENABLE_DATA_ROUTES=false|QBSVC_ENABLE_ADMIN_ROUTES=true|QBSVC_TOKEN_BACKEND=secret_manager|QBSVC_GCP_PROJECT=${GCP_PROJECT}|QBSVC_REALM_ID=${REALM_ID}|QBSVC_SECRET_NAME_TOKENS=${SECRET_TOKENS}|QBSVC_ADMIN_RETURN_URL=${RETURN_URL}|QBSVC_INTUIT_ENVIRONMENT=${INTUIT_ENV}"

if [[ -n "${EXISTING_URL}" ]]; then
  echo "==> Reusing existing service URL: ${EXISTING_URL}"
  # Fold the redirect URI into --set-env-vars on redeploys. It must NOT be added
  # as a separate --update-env-vars flag: gcloud treats --set-env-vars and
  # --update-env-vars as mutually exclusive in a single `run deploy` call.
  ENV_PAIRS="${ENV_PAIRS}|QBSVC_OAUTH_REDIRECT_URI=${EXISTING_URL}/admin/oauth/callback"
else
  echo "==> First deploy: deploying without QBSVC_OAUTH_REDIRECT_URI; will set on a second pass"
fi

DEPLOY_ARGS=(
  "${SERVICE}"
  --project="${GCP_PROJECT}"
  --region="${REGION}"
  --image="${IMAGE}"
  --platform=managed
  --allow-unauthenticated
  --service-account="${RUNTIME_SA}"
  --min-instances=0
  --max-instances=4
  --concurrency=80
  --cpu=1
  --memory=512Mi
  --timeout=60
  --port=8080
  --execution-environment=gen2
  "--set-env-vars=${ENV_PAIRS}"
  --set-secrets="QBSVC_INTUIT_CLIENT_ID=${SECRET_CLIENT_ID}:latest,QBSVC_INTUIT_CLIENT_SECRET=${SECRET_CLIENT_SECRET}:latest,QBSVC_ADMIN_LAUNCH_SECRET=${SECRET_ADMIN_LAUNCH}:latest"
)

echo "==> Deploying ${SERVICE} (PUBLIC, admin OAuth bootstrap only)"
gcloud run deploy "${DEPLOY_ARGS[@]}"

URL="$(gcloud run services describe "${SERVICE}" \
  --project="${GCP_PROJECT}" --region="${REGION}" \
  --format='value(status.url)')"

if [[ -z "${EXISTING_URL}" ]]; then
  echo "==> Setting QBSVC_OAUTH_REDIRECT_URI=${URL}/admin/oauth/callback on a follow-up revision"
  gcloud run services update "${SERVICE}" \
    --project="${GCP_PROJECT}" --region="${REGION}" \
    --update-env-vars="QBSVC_OAUTH_REDIRECT_URI=${URL}/admin/oauth/callback"
fi

echo
echo "Deployed: ${URL}"
echo "Next steps:"
echo "  1. Register ${URL}/admin/oauth/callback as a redirect URI in the Intuit console."
echo "  2. In the consuming app, point the Connect-QuickBooks button at:"
echo "       ${URL}/admin/oauth/start?launch=<launch-token>"
echo "     minting <launch-token> with the shared SECRET_ADMIN_LAUNCH value."
echo "  3. A tokenless GET ${URL}/admin/oauth/start must return 403 (gate is live)."
