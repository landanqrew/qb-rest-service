# Replicator Job — one-time setup

Stand up the GCP-side resources the env-sync replicator needs before the first
`deploy/replicate-job.deploy.sh` run: a **dedicated, isolated** runtime service
account, the sandbox token secret, and per-secret IAM bindings.

The replicator is a Cloud Run **Job** (run-to-completion), not a Service. It
copies a production realm's Accounts/Customers/Items into a freshly-reset
sandbox realm — see [`../docs/plan-env-sync.md`](../docs/plan-env-sync.md).

Run these once per environment. Each step is idempotent (existing-resource
errors are guarded with `|| true`).

> **Why a separate SA?** The Job needs read on the **source** (prod) token
> secret *and* read on the **target** (sandbox) token secret. Reusing
> `qb-service-runtime` would widen the data-API identity's blast radius to
> include the sandbox. A dedicated `qb-replicate-runtime` keeps the replicator's
> reach off the serving path.

## Variables

```bash
export GCP_PROJECT="<your project id>"
export REGION="us-central1"
export AR_REPO="qb-service"                 # reuses the qb-service image repo
export JOB="qb-replicate"
export RUNTIME_SA="qb-replicate-runtime@${GCP_PROJECT}.iam.gserviceaccount.com"
export SOURCE_SECRET="mwl-qb-tokens"          # existing — prod realm tokens
export TARGET_SECRET="mwl-qb-tokens-sandbox"  # new — sandbox realm tokens
```

## 1. Create the dedicated runtime service account

```bash
gcloud iam service-accounts create qb-replicate-runtime \
  --display-name="qb env-sync replicator runtime" \
  --project="${GCP_PROJECT}" || true
```

## 2. Create the sandbox token secret

Holds the sandbox realm's rotated token blob, exactly like `mwl-qb-tokens` does
for prod. Created empty; populated by the OAuth bootstrap in step 4.

```bash
gcloud secrets create "${TARGET_SECRET}" \
  --replication-policy=automatic \
  --project="${GCP_PROJECT}" || true
```

## 3. Grant per-secret access (read both; write the sandbox one)

The Job **reads** both token blobs to talk to each realm. It only **writes**
back to the sandbox secret — the QBClient rotates the sandbox refresh token as
it runs, and that rotation must be persisted (same invariant as the service).
It must **not** be able to write the prod token secret.

```bash
# Read source (prod) tokens.
gcloud secrets add-iam-policy-binding "${SOURCE_SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project="${GCP_PROJECT}"

# Read + write-new-version the target (sandbox) tokens.
gcloud secrets add-iam-policy-binding "${TARGET_SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project="${GCP_PROJECT}"
gcloud secrets add-iam-policy-binding "${TARGET_SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretVersionAdder" \
  --project="${GCP_PROJECT}"
```

The Job also needs the Intuit client credentials to refresh tokens. Grant read
on those two (they already exist from [`iam-setup.md`](iam-setup.md) §4):

```bash
for SECRET in mwl-qb-client-id mwl-qb-client-secret; do
  gcloud secrets add-iam-policy-binding "${SECRET}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${GCP_PROJECT}"
done
```

Do **not** grant `secretAccessor` at the project level — the per-secret
bindings are the blast-radius guarantee.

## 4. Bootstrap sandbox OAuth (populate the sandbox token secret)

The sandbox secret is empty until a token blob is written to it. Reuse the
existing **qb-admin** browser OAuth flow (no new code) pointed at the **sandbox**
realm:

1. Ensure the Intuit developer app's **Development** keys are the ones in
   `mwl-qb-client-id` / `mwl-qb-client-secret`, and the sandbox realm's redirect
   URI is registered (see [`oauth-setup.md`](oauth-setup.md)).
2. Deploy (or reuse) the qb-admin service configured to write `${TARGET_SECRET}`
   — i.e. `QBSVC_SECRET_NAME_TOKENS=mwl-qb-tokens-sandbox` and
   `QBSVC_INTUIT_ENVIRONMENT=sandbox`. See [`qb-admin-setup.md`](qb-admin-setup.md).
3. Complete `/admin/oauth/start` against the sandbox company. On success, the
   callback writes the sandbox token blob (with the sandbox `realmId`) into
   `${TARGET_SECRET}`.

> This is a one-time step per sandbox. If you later **Clear Data and Reset** the
> sandbox and its `realmId` changes, re-run this bootstrap so the secret holds
> tokens for the new realm.

## 5. Cloud Build push access

`replicate-job.deploy.sh` builds via `gcloud builds submit`. If you followed
[`iam-setup.md`](iam-setup.md) §6, Cloud Build already has
`artifactregistry.writer` on `${AR_REPO}` — nothing new here. Otherwise apply
that step.

## 6. Deploy and run

```bash
# Deploy (or update) the Job definition — safe to re-run.
GCP_PROJECT="${GCP_PROJECT}" ./deploy/replicate-job.deploy.sh

# BEFORE running: Clear Data and Reset the sandbox company in the Intuit
# Developer Portal (the replicator is create-only and aborts on a dirty target).

# Execute a run:
gcloud run jobs execute "${JOB}" \
  --project="${GCP_PROJECT}" --region="${REGION}" --wait
```

Watch progress and the final summary in the Job's logs:

```bash
gcloud run jobs executions list --job="${JOB}" \
  --project="${GCP_PROJECT}" --region="${REGION}"
```

## CI/CD note

There is no automated pipeline on `main` today; deploys are manual scripts. If
you add CI, deploy the **Job** the same way you'd deploy the services (build
image → `gcloud run jobs update`), but **never auto-execute it** — running it
mutates the sandbox and requires a manual Clear-Data-and-Reset first. Keep
`gcloud run jobs execute` a human-triggered step.

## See also

- [`replicate-job.deploy.sh`](replicate-job.deploy.sh) — the build + deploy script
- [`iam-setup.md`](iam-setup.md) — the qb-service SA/secret setup this mirrors
- [`../docs/plan-env-sync.md`](../docs/plan-env-sync.md) — replicator design + runbook
```
