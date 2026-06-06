#!/usr/bin/env bash
# Build and deploy qb-service to Cloud Run.
#
# Idempotent: re-running deploys a new revision. Routes 100% of traffic to the
# new revision once /healthz passes the startup probe.
#
# Required env vars (export before running, or pass on the command line):
#   GCP_PROJECT       GCP project ID hosting Cloud Run + Secret Manager
#   REGION            Cloud Run region (default: us-central1)
#   SERVICE           Cloud Run service name (default: qb-service)
#   AR_REPO           Artifact Registry repo name (default: qb-service)
#   REALM_ID          Intuit realm (company) ID for Martin Water Labs
#   RUNTIME_SA        Runtime service account email (default:
#                       qb-service-runtime@${GCP_PROJECT}.iam.gserviceaccount.com)
#   ADMIN_ALLOWLIST   Comma-separated emails permitted to call /admin/* (issue #13).
#                       Must include the operator who bootstraps OAuth; must NOT
#                       include the web app's runtime SA. See
#                       deploy/oauth-setup.md §"Admin gate".
#   INTUIT_ENV        "production" or "sandbox" (default: production). Sandbox
#                       points the QBO client at sandbox-quickbooks.api.intuit.com
#                       and must pair with the app's Development keys + a
#                       sandbox realm ID.
#
# Prerequisites (see deploy/iam-setup.md):
#   - Artifact Registry repo ${AR_REPO} exists in ${REGION}
#   - Runtime service account exists and has
#       roles/secretmanager.secretAccessor on mwl-qb-client-id,
#       mwl-qb-client-secret, and mwl-qb-tokens
#   - Secrets mwl-qb-client-id and mwl-qb-client-secret already populated
#   - Secret mwl-qb-tokens exists (can be empty; bootstrapped via /admin/oauth/start)
#   - The Intuit OAuth redirect URI is registered in the developer console
#     for the eventual Cloud Run service URL.

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:?GCP_PROJECT is required}"
REALM_ID="${REALM_ID:?REALM_ID is required}"
ADMIN_ALLOWLIST="${ADMIN_ALLOWLIST:?ADMIN_ALLOWLIST is required (comma-separated admin emails; see deploy/oauth-setup.md)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-qb-service}"
AR_REPO="${AR_REPO:-qb-service}"
RUNTIME_SA="${RUNTIME_SA:-qb-service-runtime@${GCP_PROJECT}.iam.gserviceaccount.com}"
INTUIT_ENV="${INTUIT_ENV:-production}"

# Fail before the build/deploy cycle on a bad value — Settings would reject
# it at container start (pattern ^(production|sandbox)$) after minutes of
# waiting on Cloud Build.
if [[ ! "${INTUIT_ENV}" =~ ^(production|sandbox)$ ]]; then
  echo "ERROR: INTUIT_ENV must be 'production' or 'sandbox', got: ${INTUIT_ENV}" >&2
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${SERVICE}:${GIT_SHA}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Building image ${IMAGE}"
# cloudbuild.yaml forces BuildKit — the default --tag path uses the legacy
# Docker builder, which can't parse this Dockerfile (ARG in COPY --from,
# RUN --mount caches).
gcloud builds submit "${REPO_ROOT}" \
  --project="${GCP_PROJECT}" \
  --config="${REPO_ROOT}/cloudbuild.yaml" \
  --substitutions="_IMAGE=${IMAGE}"

# First deploy: derive the service URL after the fact. On subsequent deploys
# we reuse the existing URL so the Intuit-registered OAuth redirect stays
# byte-identical across revisions.
EXISTING_URL="$(gcloud run services describe "${SERVICE}" \
  --project="${GCP_PROJECT}" --region="${REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"

DEPLOY_ARGS=(
  "${SERVICE}"
  --project="${GCP_PROJECT}"
  --region="${REGION}"
  --image="${IMAGE}"
  --platform=managed
  --no-allow-unauthenticated
  --service-account="${RUNTIME_SA}"
  --min-instances=0
  --concurrency=80
  --cpu=1
  --memory=512Mi
  --timeout=60
  --port=8080
  --execution-environment=gen2
  # ADMIN_ALLOWLIST is exported with `^|^` as the delimiter so the comma-
  # separated email list can pass through `--set-env-vars` without being
  # mis-parsed as multiple env vars.
  "--set-env-vars=^|^QBSVC_TOKEN_BACKEND=secret_manager|QBSVC_GCP_PROJECT=${GCP_PROJECT}|QBSVC_REALM_ID=${REALM_ID}|QBSVC_SECRET_NAME_TOKENS=mwl-qb-tokens|QBSVC_ADMIN_ALLOWLIST=${ADMIN_ALLOWLIST}|QBSVC_INTUIT_ENVIRONMENT=${INTUIT_ENV}"
  --set-secrets="QBSVC_INTUIT_CLIENT_ID=mwl-qb-client-id:latest,QBSVC_INTUIT_CLIENT_SECRET=mwl-qb-client-secret:latest"
)

if [[ -n "${EXISTING_URL}" ]]; then
  echo "==> Reusing existing service URL: ${EXISTING_URL}"
  DEPLOY_ARGS+=( --update-env-vars="QBSVC_OAUTH_REDIRECT_URI=${EXISTING_URL}/admin/oauth/callback" )
else
  echo "==> First deploy: deploying without QBSVC_OAUTH_REDIRECT_URI; will set on a second pass"
fi

echo "==> Deploying ${SERVICE} (image ${IMAGE})"
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
echo "  1. Register ${URL}/admin/oauth/callback in the Intuit developer console."
echo "  2. Browse to ${URL}/admin/oauth/start with an admin identity that has"
echo "     roles/run.invoker to complete the OAuth bootstrap."
# /readyz, not /healthz: Google's frontend intercepts the literal /healthz
# path on run.app domains (404 before the container sees it).
echo "  3. Verify: curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${URL}/readyz"
echo "     (503 NOT_AUTHENTICATED is expected until the OAuth bootstrap in step 2.)"
