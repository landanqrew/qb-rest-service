#!/usr/bin/env bash
# Build and deploy the QBO env-sync replicator as a Cloud Run Job.
#
# The replicator (scripts/replicate) copies a production realm's Accounts,
# Customers, and Items into a freshly-reset sandbox realm — see
# docs/plan-env-sync.md. It is an out-of-band operational tool, NOT part of the
# qb-service HTTP surface, so it runs as a Cloud Run *Job* (run-to-completion,
# no request timeout) rather than a Service.
#
# It reuses the SAME container image as qb-service; only the entrypoint is
# overridden to `python -m scripts.replicate`. Because prod tokens live in
# Secret Manager, the Job runs in-network and reads them via its runtime SA.
#
# Two token secrets, one per realm:
#   mwl-qb-tokens           source (production realm) — the existing secret
#   mwl-qb-tokens-sandbox   target (sandbox realm)    — bootstrap once via the
#                             qb-admin OAuth flow against the sandbox realm
#
# Required env vars:
#   GCP_PROJECT   GCP project ID
#   REGION        Cloud Run region (default: us-central1)
#   JOB           Cloud Run Job name (default: qb-replicate)
#   AR_REPO       Artifact Registry repo (default: qb-service)
#   IMAGE         Full image ref to run (default: build from current git SHA)
#   RUNTIME_SA    Runtime SA (default: qb-replicate-runtime@${GCP_PROJECT}...)
#
# The runtime SA needs roles/secretmanager.secretAccessor on BOTH token secrets.
#
# After deploy, run it with:
#   gcloud run jobs execute "${JOB}" --project="${GCP_PROJECT}" --region="${REGION}" --wait

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:?GCP_PROJECT is required}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-qb-replicate}"
AR_REPO="${AR_REPO:-qb-service}"
RUNTIME_SA="${RUNTIME_SA:-qb-replicate-runtime@${GCP_PROJECT}.iam.gserviceaccount.com}"
SOURCE_SECRET="${SOURCE_SECRET:-mwl-qb-tokens}"
TARGET_SECRET="${TARGET_SECRET:-mwl-qb-tokens-sandbox}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GIT_SHA="$(git rev-parse --short HEAD)"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/qb-replicate:${GIT_SHA}}"

if [[ -z "${SKIP_BUILD:-}" ]]; then
  echo "==> Building image ${IMAGE}"
  gcloud builds submit "${REPO_ROOT}" \
    --project="${GCP_PROJECT}" \
    --config="${REPO_ROOT}/cloudbuild.yaml" \
    --substitutions="_IMAGE=${IMAGE}"
fi

# `create` first time, `update` after — try update, fall back to create so the
# script is idempotent.
DEPLOY_VERB="update"
if ! gcloud run jobs describe "${JOB}" \
      --project="${GCP_PROJECT}" --region="${REGION}" >/dev/null 2>&1; then
  DEPLOY_VERB="create"
fi

echo "==> ${DEPLOY_VERB} Cloud Run Job ${JOB}"
gcloud run jobs "${DEPLOY_VERB}" "${JOB}" \
  --project="${GCP_PROJECT}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --service-account="${RUNTIME_SA}" \
  --command="python" \
  --args="-m,scripts.replicate,--backend,secret_manager,--gcp-project,${GCP_PROJECT},--source-secret,${SOURCE_SECRET},--target-secret,${TARGET_SECRET},--summary,/tmp/replication-summary.json" \
  --set-env-vars="QBSVC_GCP_PROJECT=${GCP_PROJECT}" \
  --set-secrets="QBSVC_INTUIT_CLIENT_ID=mwl-qb-client-id:latest,QBSVC_INTUIT_CLIENT_SECRET=mwl-qb-client-secret:latest" \
  --max-retries=0 \
  --task-timeout=3600 \
  --cpu=1 \
  --memory=512Mi

echo "==> Done. Execute with:"
echo "    gcloud run jobs execute ${JOB} --project=${GCP_PROJECT} --region=${REGION} --wait"
