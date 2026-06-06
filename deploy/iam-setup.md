# IAM & Secret Manager — one-time setup

Stand up the GCP-side resources qb-service needs before the first
`deploy/deploy.sh` run: a dedicated runtime service account, three
Secret Manager secrets, and the IAM bindings that scope the runtime SA
to those secrets only.

Run these once per environment. They are safe to re-run (each step is
idempotent; existing-resource errors are tolerated).

## Variables

```bash
export GCP_PROJECT="<your project id>"
export REGION="us-central1"          # or wherever MWL infra lives
export SERVICE="qb-service"
export AR_REPO="qb-service"
export RUNTIME_SA="qb-service-runtime@${GCP_PROJECT}.iam.gserviceaccount.com"
# Identities allowed to invoke the data routes (Lab Intake web app):
export CALLER_SA="<web-app-runtime-sa>@<web-app-project>.iam.gserviceaccount.com"
# Identity allowed to bootstrap OAuth (your personal Google identity):
export ADMIN_USER="user:<you>@<domain>"
```

## 1. Enable the APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  --project="${GCP_PROJECT}"
```

## 2. Create the Artifact Registry repo (one-time)

```bash
gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="Container images for ${SERVICE}" \
  --project="${GCP_PROJECT}" || true
```

## 3. Create the runtime service account

```bash
gcloud iam service-accounts create qb-service-runtime \
  --display-name="qb-service runtime" \
  --project="${GCP_PROJECT}" || true
```

## 4. Create the three secrets

`mwl-qb-client-id` and `mwl-qb-client-secret` hold the Intuit OAuth app
credentials (one value per secret so Cloud Run can mount each into a
separate env var). `mwl-qb-tokens` holds the rotated refresh+access
token blob; the runtime writes a new version to it on every refresh.

`gcloud secrets create` errors with `ALREADY_EXISTS` if you re-run it, so each
`create` is guarded with `|| true`. The subsequent `versions add` calls always
append a new version, so it's safe to re-run them to rotate a value.

```bash
# Intuit client ID
gcloud secrets create mwl-qb-client-id \
  --replication-policy=automatic \
  --project="${GCP_PROJECT}" || true
printf '%s' '<INTUIT_CLIENT_ID>' | gcloud secrets versions add mwl-qb-client-id \
  --data-file=- --project="${GCP_PROJECT}"

# Intuit client secret
gcloud secrets create mwl-qb-client-secret \
  --replication-policy=automatic \
  --project="${GCP_PROJECT}" || true
printf '%s' '<INTUIT_CLIENT_SECRET>' | gcloud secrets versions add mwl-qb-client-secret \
  --data-file=- --project="${GCP_PROJECT}"

# Token blob (created empty; populated by /admin/oauth/callback on first auth)
gcloud secrets create mwl-qb-tokens \
  --replication-policy=automatic \
  --project="${GCP_PROJECT}" || true
```

> **Note on naming.** The scope doc and issue #12 refer to the client
> credentials as a single logical secret `mwl-qb-client`. We split it
> into two GCP secrets (`mwl-qb-client-id`, `mwl-qb-client-secret`)
> because Cloud Run's `--set-secrets` binds one secret per env var.

## 5. Grant the runtime SA Secret Manager access — only on these three

```bash
for SECRET in mwl-qb-client-id mwl-qb-client-secret mwl-qb-tokens; do
  gcloud secrets add-iam-policy-binding "${SECRET}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${GCP_PROJECT}"
done

# Token blob is the only secret the runtime writes to (rotated refresh tokens).
gcloud secrets add-iam-policy-binding mwl-qb-tokens \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretVersionAdder" \
  --project="${GCP_PROJECT}"
```

Do **not** grant `roles/secretmanager.secretAccessor` at the project
level. The deliberate per-secret bindings are the blast-radius guarantee.

## 6. Allow Cloud Build to push to Artifact Registry

`deploy.sh` uses `gcloud builds submit` which runs as the Cloud Build
service account. Grant it write on the repo:

```bash
CLOUDBUILD_SA="$(gcloud projects describe "${GCP_PROJECT}" \
  --format='value(projectNumber)')@cloudbuild.gserviceaccount.com"

gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" \
  --location="${REGION}" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/artifactregistry.writer" \
  --project="${GCP_PROJECT}"
```

## 7. Invoker IAM on the deployed service

`deploy.sh` deploys with `--no-allow-unauthenticated`, so all callers
need `roles/run.invoker` explicitly.

After the first deploy succeeds:

```bash
# Lab Intake web app's runtime SA → can hit the data routes
gcloud run services add-iam-policy-binding "${SERVICE}" \
  --region="${REGION}" \
  --member="serviceAccount:${CALLER_SA}" \
  --role="roles/run.invoker" \
  --project="${GCP_PROJECT}"

# You → can hit /admin/oauth/* to bootstrap the refresh token
gcloud run services add-iam-policy-binding "${SERVICE}" \
  --region="${REGION}" \
  --member="${ADMIN_USER}" \
  --role="roles/run.invoker" \
  --project="${GCP_PROJECT}"
```

> Cloud Run IAM is service-level, not path-level — so the web app's SA
> would otherwise be able to reach `/admin/oauth/*` too. The
> `AdminGateMiddleware` (issue #13) enforces the split inside the app via
> `QBSVC_ADMIN_ALLOWLIST`. Make sure the env var includes the admin
> identity bound here; the web-app SA must **not** be on it. See
> [`oauth-setup.md`](oauth-setup.md) §"Admin gate".

## 8. Verify

```bash
URL="$(gcloud run services describe "${SERVICE}" \
  --project="${GCP_PROJECT}" --region="${REGION}" \
  --format='value(status.url)')"

# No auth → 403
curl -i "${URL}/healthz"

# With a valid ID token → 200
curl -i -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "${URL}/healthz"
```

Once `/healthz` returns 200, complete the OAuth bootstrap per
[`oauth-setup.md`](oauth-setup.md) §3.

## 9. qb-pages runtime service account (Amendment 2)

The public static-pages companion service ([`deploy-pages.sh`](deploy-pages.sh))
deploys with a dedicated SA that has **no role bindings** — it serves three
static HTML files, reads no secrets, and calls nothing.

```bash
gcloud iam service-accounts create qb-pages-runtime \
  --display-name="qb-pages runtime (no permissions)" \
  --project="${GCP_PROJECT}" || true
```

Do not bind any roles to it. qb-pages is deployed `--allow-unauthenticated`
on purpose — Intuit's production-app constraints require publicly resolvable
landing/EULA/privacy URLs — and the empty SA keeps the blast radius of
"public" at those three files. See
[`../docs/qb-service-scope.md`](../docs/qb-service-scope.md) §14.

## See also

- [`oauth-setup.md`](oauth-setup.md) — Intuit dev console setup and OAuth flow
- [`cloud-run.yaml`](cloud-run.yaml) — declarative form of what `deploy.sh` produces
- [`qb-pages.cloud-run.yaml`](qb-pages.cloud-run.yaml) / [`deploy-pages.sh`](deploy-pages.sh) — public pages companion service
- [`../docs/qb-service-scope.md`](../docs/qb-service-scope.md) §7 (auth), §14 (deployment)
