# QBO Environment Sync (prod → sandbox replication)

**Status:** Implemented (v1) · Accounts + Customers + Items · Invoices future scope

## Why

To validate the Lab Intake web app (and this service) against *realistic* QBO
data, a staging/sandbox QBO realm must be populated with copies of the
**production** company's data. Intuit's generic sandbox seed data doesn't
resemble a real customer's naming, sub-customer usage, or item/account
structure, so testing against it proves little. QBO offers **no native
prod→sandbox clone**, so we replicate with a custom tool.

## Shape

A **standalone out-of-band tool** under [`scripts/replicate/`](../scripts/replicate/) —
**not** part of the deployed FastAPI service (the service stays stateless,
single-realm, data-only). It reuses `qbsvc`'s QBO plumbing (`QBClient`,
`build_query`, token stores) and talks to **two realms at once** via two token
stores. It runs as a **Cloud Run Job** because production OAuth tokens live in
Secret Manager and must be read in-network.

**Full-refresh model:** the operator manually does **Clear Data and Reset** on
the sandbox in the Intuit Developer Portal first; the tool is **create-only**
(never deletes) and **aborts with instructions** if it detects a non-empty
target.

## How it works

Copies three entity tiers in dependency order — a child can't reference a
parent Id that doesn't exist yet in the target:

    Accounts  →  Customers  →  Items

and *within* each hierarchical type, parents before children (topological sort
on `ParentRef`). Accounts come first because every Item needs an
`IncomeAccountRef`.

**ID remapping** ([`mapping.py`](../scripts/replicate/mapping.py)) is the
correctness core. When an entity is copied, QBO mints a *new* Id in the target,
so every reference must be rewritten from the prod Id to the new sandbox Id.
The policy is per-ref-type:

| Ref | Action |
|---|---|
| `ParentRef`, `IncomeAccountRef`, `ExpenseAccountRef`, `AssetAccountRef` | **Remap** (numeric Ids that change across realms) |
| `CurrencyRef` (`"USD"`), `TaxCodeRef` sentinels (`"TAX"`/`"NON"`) | **Pass through** (realm-independent; remapping would corrupt them) |

A ref pointing at a prod Id that was never replicated (e.g. an Item whose
account was inactive and filtered out) is recorded as a **skipped** record with
a reason — best-effort dataset over aborting the whole run.

### Module map

| File | Role |
|---|---|
| [`mapping.py`](../scripts/replicate/mapping.py) | `IdMap`, per-ref remapping policy, `topo_sort_by_parent` (pure, no QBO) |
| [`reader.py`](../scripts/replicate/reader.py) | Exhaustive paged reads via `build_query` + `QBClient.query` |
| [`writer.py`](../scripts/replicate/writer.py) | Per-entity payload builders + the single `create()` choke-point |
| [`precheck.py`](../scripts/replicate/precheck.py) | Fresh-sandbox detection + instructional abort |
| [`orchestrator.py`](../scripts/replicate/orchestrator.py) | Drives the three tiers, tallies created/skipped, builds the id-maps |
| [`clients.py`](../scripts/replicate/clients.py) | Builds the two realm-bound `QBClient`s (Secret Manager or file backend) |
| [`__main__.py`](../scripts/replicate/__main__.py) | CLI entrypoint, summary write, exit codes |

The writer routes **every** create through one `create()` method, so a QBO
`/batch` backend (30 ops/call — the performance lever the 10k-row Invoice tier
will want) can be added there without touching the orchestrator. v1 is
sequential, respecting each client's own `TokenBucket` (~480/min per realm).

## One-time setup

Full runbook: [`deploy/replicate-job-setup.md`](../deploy/replicate-job-setup.md).
In brief:

1. **Dedicated runtime SA** (`qb-replicate-runtime@…`) — isolated from the
   data-API SA so the sandbox-write reach stays off the serving path.
2. **Sandbox token secret** (`mwl-qb-tokens-sandbox`) — new, empty until step 4.
3. **Per-secret IAM:** read `mwl-qb-tokens` (source) + `mwl-qb-client-*`;
   read **and** write `mwl-qb-tokens-sandbox` (the Job rotates the sandbox
   refresh token as it runs and must persist it).
4. **Bootstrap sandbox OAuth:** run the existing **qb-admin** browser OAuth flow
   against the **sandbox** realm so the sandbox secret holds a valid token blob.
   No new OAuth code — reuses `/admin/oauth/*`.
5. **Deploy the Job:** `deploy/replicate-job.deploy.sh` (see below).

## Running it

**Every run, first:** in the Intuit Developer Portal → your app → Sandbox,
click **Clear Data and Reset** on the sandbox company. Then:

```sh
# Deploy (or update) the Cloud Run Job:
GCP_PROJECT=<project> ./deploy/replicate-job.deploy.sh

# Execute a replication run:
gcloud run jobs execute qb-replicate \
  --project=<project> --region=us-central1 --wait
```

The Job prints progress and a final summary to its logs and writes a JSON
run-summary (id-maps + per-tier counts + skipped records with reasons).

### Logs & debugging

The tool emits **structured JSON logs** (the same formatter the service uses),
so Cloud Logging parses each line into queryable fields. Key events:

| `message` | Level | Notable fields |
|---|---|---|
| `replicate_run_start` / `replicate_run_done` | INFO | `created_count`, `skipped_count` |
| `replicate_precheck_ok` / `replicate_precheck_failed` | INFO/ERROR | `target_customers`, `target_items` |
| `replicate_tier_start` / `replicate_tier_done` | INFO | `entity`, `created_count`, `skipped_count` |
| `replicate_created` | INFO | `entity`, `prod_id`, `sandbox_id` |
| `replicate_skipped` | WARNING | `entity`, `prod_id`, `skip_kind`, `intuit_tid`, `qbo_errors[].code` |
| `qbo_call` (logger `qbsvc.qbo`) | INFO | `method`, `qbo_endpoint`, `status`, `qbo_duration_ms`, `intuit_tid` |
| `replicate_run_failed` | ERROR | full `exception` traceback |

Every QBO rejection carries Intuit's **`intuit_tid`** and the **QBO fault
code** (6240 duplicate, 5010 stale token, …) — the two things needed to
diagnose it. The same detail is embedded per skipped record in the summary JSON
under `qbo`.

**To hand back a run for debugging**, send:
1. The **run summary JSON** (`--summary` path) — has the id-maps, counts, and
   every skipped record with its fault code + `intuit_tid`.
2. The Cloud Logging lines for the failing execution — filter to
   `jsonPayload.message=~"replicate_" OR jsonPayload.message="qbo_call"`, or
   just grab all logs for the execution:
   ```sh
   gcloud logging read \
     'resource.type=cloud_run_job AND resource.labels.job_name=qb-replicate' \
     --project=<project> --limit=500 --format=json
   ```
3. On an unexpected crash, the `replicate_run_failed` line carries the full
   traceback.

`--log-level DEBUG` keeps the per-call `qbo_call` lines; noisy httpx
request-URL logging is silenced below WARNING regardless.

**Local dev** (two token JSON files, no GCP):

```sh
python -m scripts.replicate --backend file \
  --source-tokens ./prod-tokens.json \
  --target-tokens ./sandbox-tokens.json \
  --summary ./replication-summary.json
```

**Exit codes:** `0` success (check summary for skipped records) · `2` target
not freshly reset (precheck abort) · `1` unexpected failure.

The fresh-sandbox precheck is count-based (a reset sandbox has few
Customers/Items; a populated one has many). The threshold is
`--seed-threshold` (default 50); `--force` bypasses the precheck entirely.

## Tests

- [`tests/unit/test_replicate_mapping.py`](../tests/unit/test_replicate_mapping.py) —
  remapping policy (remap numeric refs, pass through sentinels) + topo sort +
  cycle detection, pure.
- [`tests/integration/test_replicate_flow.py`](../tests/integration/test_replicate_flow.py) —
  full three-tier replication against a mock QBO transport; asserts the exact
  remapped bodies and parent-before-child ordering.
- [`tests/integration/test_replicate_cli.py`](../tests/integration/test_replicate_cli.py) —
  the CLI entrypoint end to end (summary file, exit codes, precheck abort).

## Deliberately deferred

- **Invoice replication** — future scope; the writer choke-point is batch-ready
  for the 10k-row volume it'll bring.
- **QBO `/batch` support** — the performance lever, added when Invoices arrive.
- **Terms / PaymentMethods / TaxCodes** replication — only if source Customers
  actually populate those refs (revisit when reads show they do).
- **GCS/Firestore run history** — the local JSON summary suffices for a
  manually-run Job.
