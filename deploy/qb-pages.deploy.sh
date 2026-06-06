#!/usr/bin/env bash
# Build and deploy qb-pages (the public static-pages companion) to Cloud Run.
#
# qb-pages is the deliberate opposite of qb-service: it is PUBLIC and holds
# nothing. It serves three static HTML pages so Intuit's production-app review
# has real, resolvable landing/EULA/privacy URLs.
#
# Hard guarantees this script keeps (scope doc Amendment 2):
#   - Deployed with --allow-unauthenticated (public).
#   - A dedicated runtime service account with ZERO IAM grants. This script
#     never grants it any role; if it does not exist yet, create it (see below)
#     and leave it permission-less.
#   - No credentials wired in: the deploy never passes any secret-mounting flag.
#   - No QBO env wiring.
#
# Required env vars:
#   GCP_PROJECT   GCP project ID hosting Cloud Run
# Optional (with defaults):
#   REGION        Cloud Run region (default: us-central1)
#   SERVICE       Service name (default: qb-pages)
#   AR_REPO       Artifact Registry repo (default: qb-pages)
#   RUNTIME_SA    Permission-less runtime SA (default:
#                   qb-pages-runtime@${GCP_PROJECT}.iam.gserviceaccount.com)
#
# One-time prerequisite (create the permission-less SA, grant it NOTHING):
#   gcloud iam service-accounts create qb-pages-runtime \
#     --project="${GCP_PROJECT}" \
#     --display-name="qb-pages public static site (no permissions)"
#
# Prerequisite: Artifact Registry repo ${AR_REPO} exists in ${REGION}.

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:?GCP_PROJECT is required}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-qb-pages}"
AR_REPO="${AR_REPO:-qb-pages}"
RUNTIME_SA="${RUNTIME_SA:-qb-pages-runtime@${GCP_PROJECT}.iam.gserviceaccount.com}"

GIT_SHA="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${SERVICE}:${GIT_SHA}"

# Build context is the web/ directory ONLY — nothing from the rest of the repo.
WEB_DIR="$(cd "$(dirname "$0")/../web" && pwd)"

echo "==> Building image ${IMAGE} from ${WEB_DIR}"
gcloud builds submit "${WEB_DIR}" \
  --project="${GCP_PROJECT}" \
  --tag="${IMAGE}"

echo "==> Deploying ${SERVICE} (public, no secrets, permission-less SA)"
gcloud run deploy "${SERVICE}" \
  --project="${GCP_PROJECT}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --platform=managed \
  --allow-unauthenticated \
  --service-account="${RUNTIME_SA}" \
  --min-instances=0 \
  --max-instances=4 \
  --concurrency=80 \
  --cpu=1 \
  --memory=128Mi \
  --timeout=30 \
  --port=8080 \
  --execution-environment=gen2

URL="$(gcloud run services describe "${SERVICE}" \
  --project="${GCP_PROJECT}" --region="${REGION}" \
  --format='value(status.url)')"

echo
echo "Deployed: ${URL}"
echo "Public URLs to register in the Intuit developer console:"
echo "  Host (launch) URL : ${URL}/"
echo "  EULA URL          : ${URL}/eula"
echo "  Privacy policy URL: ${URL}/privacy"
echo
echo "Smoke test (no auth — these are public):"
echo "  curl -sS -o /dev/null -w '%{http_code}\\n' ${URL}/"
echo "  curl -sS -o /dev/null -w '%{http_code}\\n' ${URL}/eula"
echo "  curl -sS -o /dev/null -w '%{http_code}\\n' ${URL}/privacy"
