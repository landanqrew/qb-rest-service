#!/usr/bin/env bash
# Build and deploy qb-pages — the public static-pages companion service
# (landing page, EULA, privacy policy) that satisfies Intuit's
# production-app URL requirements. See docs/qb-service-scope.md §14
# (Amendment 2) and issue #36.
#
# Unlike qb-service, this deploys --allow-unauthenticated: public
# reachability is the point. The runtime SA must have ZERO role bindings —
# the pages are static, so the blast radius of "public" stays three HTML
# files. See deploy/iam-setup.md §9.
#
# Required env vars:
#   GCP_PROJECT   GCP project ID
# Optional:
#   REGION        Cloud Run region (default: us-central1)
#   SERVICE       Cloud Run service name (default: qb-pages)
#   AR_REPO       Artifact Registry repo (default: qb-service — shared)
#   RUNTIME_SA    Runtime service account email (default:
#                   qb-pages-runtime@${GCP_PROJECT}.iam.gserviceaccount.com)

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:?GCP_PROJECT is required}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-qb-pages}"
AR_REPO="${AR_REPO:-qb-service}"
RUNTIME_SA="${RUNTIME_SA:-qb-pages-runtime@${GCP_PROJECT}.iam.gserviceaccount.com}"

GIT_SHA="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${SERVICE}:${GIT_SHA}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Building image ${IMAGE}"
gcloud builds submit "${REPO_ROOT}/web" \
  --project="${GCP_PROJECT}" \
  --tag="${IMAGE}"

echo "==> Deploying ${SERVICE} (image ${IMAGE})"
gcloud run deploy "${SERVICE}" \
  --project="${GCP_PROJECT}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --platform=managed \
  --allow-unauthenticated \
  --service-account="${RUNTIME_SA}" \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=80 \
  --cpu=1 \
  --memory=256Mi \
  --timeout=30 \
  --port=8080 \
  --execution-environment=gen2

URL="$(gcloud run services describe "${SERVICE}" \
  --project="${GCP_PROJECT}" --region="${REGION}" \
  --format='value(status.url)')"

echo
echo "Deployed: ${URL}"
echo "Intuit developer console URLs (issue #38):"
echo "  Host / launch URL:  ${URL}/"
echo "  EULA URL:           ${URL}/eula"
echo "  Privacy policy URL: ${URL}/privacy"
