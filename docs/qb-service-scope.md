# QB Service — Project Scope (Option B)

**Status:** Draft for review · 2026-05-21 · Amended 2026-06-06 (Amendment 1: full CRUD surface; Amendment 2: public pages service for Intuit production requirements) · Amended 2026-08-04 (Amendment 3: invoice custom-field pass-through — see amendment log)
**Decision:** Locked on Option B (thin stateless REST proxy in front of QBO)
**Repo plan:** Clone `quickbooks-cli` → new repo (working name `qb-service`). CLI stays where it is.

---

## 1. Purpose

A small HTTP service that fronts the QuickBooks Online API for a consuming web app. It owns OAuth, token refresh, rate-limit handling, and the QBO request/response quirks. It holds no business data — the consuming app's database remains the source of truth for jobs/samples/orders.

## 2. Goals

- One place owns QBO auth + the REST client; web app never sees an Intuit token.
- Web app can `GET` entities (customers, items, invoices), manage items (create/update/deactivate), and create/update/delete invoices and their lines through a small, stable HTTP surface. *(Amendment 1 — originally read + invoice `POST/PUT` only.)*
- Deployable to GCP (Cloud Run preferred) with IAM-based service-to-service auth.
- Reuse ~70% of the existing `qb/` codebase (client, auth flow, error handling, query builder).

## 3. Non-goals

- **No business logic.** No invoice numbering, no test-code → item mapping, no sample lifecycle. Those live in the web app.
- **No database.** No caching layer, no warm pull. Every request hits QBO live. (If read latency becomes a problem, that's the trigger to revisit Option C — not now.)
- **No CLI features.** Payments, estimates, vendors, bills, P&L, table output — all stay in `quickbooks-cli`. The service exposes only what the consuming app needs.
- **No webhook ingestion** (Option D territory).
- **No multi-tenant SaaS shape.** One deployment, one QBO realm (the operator's QuickBooks company). Revisit only if the operator onboards a second QBO company.

## 4. Architecture

```
Consuming Web App  ──ID token──▶  qb-service (Cloud Run)  ──OAuth──▶  QuickBooks Online
                                          │
                                          ├─ reads:  client_id/secret  from Secret Manager
                                          └─ reads+writes: refresh token  to Secret Manager
```

- **Runtime:** Cloud Run (containerized FastAPI). Cloud Functions is viable but Cloud Run gives better control over cold starts and concurrency.
- **Public surface:** HTTPS only; no unauthenticated routes except `/healthz`.
- **Statelessness:** zero per-request state. In-memory caches limited to (a) the current access token, (b) connection pool.

## 5. Repo structure (proposed)

```
qb-service/
├── pyproject.toml
├── Dockerfile
├── README.md
├── src/
│   └── qbsvc/
│       ├── __init__.py
│       ├── main.py              # FastAPI app, lifespan, route registration
│       ├── config.py            # env-driven settings (pydantic-settings)
│       ├── api/
│       │   ├── client.py        # ← ported from qb/api/client.py
│       │   ├── pagination.py    # ← ported
│       │   └── queries.py       # ← ported
│       ├── auth/
│       │   ├── oauth.py         # ← ported, bootstrap-only path stripped from runtime
│       │   ├── tokens.py        # NEW: TokenStore protocol
│       │   └── secret_manager.py # NEW: SecretManagerTokenStore impl
│       ├── routes/
│       │   ├── customers.py
│       │   ├── items.py
│       │   ├── invoices.py
│       │   └── health.py
│       ├── schemas.py           # pydantic request/response models
│       ├── errors.py            # exception handlers, error envelope
│       └── deps.py              # FastAPI dependencies (auth, client)
├── tests/
│   ├── test_routes_*.py
│   ├── test_token_store.py
│   └── conftest.py              # httpx mock transport
└── deploy/
    ├── cloud-run.yaml
    └── oauth-setup.md           # Intuit dev console callback URL setup
```

### What ports cleanly from qb-cli

| File | Status |
|---|---|
| `qb/api/client.py` | Port as-is; replace keyring token load with `TokenStore` dependency |
| `qb/api/pagination.py` | Port as-is |
| `qb/api/queries.py` | Port as-is |
| `qb/exceptions.py` | Port as-is |
| `qb/auth/oauth.py` | Port both the **login** (authorize URL + code exchange) and **refresh** paths; replace the localhost callback server with FastAPI routes |

### What's new

| Component | Purpose |
|---|---|
| `TokenStore` protocol | Pluggable storage (Secret Manager runtime; in-memory for tests) |
| `SecretManagerTokenStore` | Read client creds + refresh token; write rotated refresh token as a new secret version |
| FastAPI routes | Thin handlers that delegate to `QBClient` |
| `schemas.py` | Pydantic models for request validation (especially invoice create/update) |
| `errors.py` | Translate `APIError`, `AuthError`, `RateLimitError` → HTTP status + JSON envelope |
| Dockerfile + Cloud Run config | Deployment |

## 6. HTTP surface (v1)

All routes return JSON. All non-GET routes require a valid Google ID token (Cloud Run IAM).

Data routes are versioned under `/v1/` (see §16 #6). Operational routes —
`/healthz`, `/readyz`, and `/admin/oauth/*` — are intentionally unversioned:
health probes are not part of the API contract, and the OAuth callback path is
registered byte-for-byte in the Intuit developer console, so it must stay stable.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness — no QBO call |
| GET | `/readyz` | Readiness — verifies token refresh works |
| GET | `/v1/customers` | List customers. Query: `active`, `modified_since`, `limit`, `cursor` |
| GET | `/v1/customers/{id}` | Single customer |
| GET | `/v1/items` | List items/products. Query: `active`, `modified_since`, `limit`, `cursor` |
| GET | `/v1/items/{id}` | Single item |
| POST | `/v1/items` | Create item. Body: `{name, type, income_account_id, unit_price?, ...}` *(Amendment 1)* |
| PUT | `/v1/items/{id}` | Sparse update, SyncToken-aware *(Amendment 1)* |
| DELETE | `/v1/items/{id}` | Deactivate (`Active: false`) — QBO cannot hard-delete items *(Amendment 1)* |
| GET | `/v1/invoices` | List invoices. Query: `customer_id`, `doc_number`, `modified_since`, `limit`, `cursor` |
| GET | `/v1/invoices/{id}` | Single invoice |
| POST | `/v1/invoices` | Create invoice. Body: `{customer_id, doc_number, txn_date?, lines[], memo?, customer_memo?, custom_fields[]?}` — `custom_fields[]` is `{definition_id, value}` *(Amendment 3)* |
| POST | `/v1/invoices/{id}/lines` | Append a line. Body: `{item_id, qty, rate?, description?}` |
| PUT | `/v1/invoices/{id}` | Full replace, SyncToken-aware *(Amendment 1 — previously deferred)* |
| DELETE | `/v1/invoices/{id}` | Delete invoice (QBO `operation=delete`), SyncToken-aware *(Amendment 1)* |
| POST | `/v1/invoices/{id}/void` | Void invoice (QBO `operation=void` — record survives at $0), SyncToken-aware *(Amendment 1)* |
| PUT | `/v1/invoices/{id}/lines/{line_id}` | Update one line — read-modify-write full update of the parent invoice *(Amendment 1)* |
| DELETE | `/v1/invoices/{id}/lines/{line_id}` | Remove one line — same read-modify-write mechanics *(Amendment 1)* |
| GET | `/admin/oauth/start` | Begin Intuit OAuth flow (admin-gated, see §7) |
| GET | `/admin/oauth/callback` | Intuit redirect target; exchanges code, writes refresh token to Secret Manager |

### Response envelope

```json
{
  "data": { ... }            // or [ ... ] for lists
  "pagination": { "next_cursor": "...", "has_more": true }   // list endpoints only
}
```

### Error envelope

```json
{
  "error": {
    "code": "QBO_VALIDATION",
    "message": "DocNumber 26-02-71 already exists",
    "qbo_detail": "..."          // pass-through from Intuit when available
  }
}
```

HTTP status codes mirror QBO where meaningful (400, 401, 404, 409, 429), with 502 reserved for unexpected upstream failures.

## 7. Auth

### Layer 1 — Web app → qb-service
- Cloud Run with `--no-allow-unauthenticated`.
- Web app's runtime service account granted `roles/run.invoker` on the qb-service.
- Web app mints ID token via metadata server; sends as `Authorization: Bearer <token>`.
- GCP verifies the JWT before our code runs. No code change required beyond enabling IAM.
- **Admin routes (`/admin/oauth/*`)** are gated more tightly: only specific Google identities (the project admin — you) get `roles/run.invoker` for them. The web app's service account does **not**. Enforced via a second Cloud Run service or path-level IAM (TBD during Phase 4).

### Layer 2 — qb-service → QBO
- Client ID + secret loaded from Secret Manager (`SECRET_CLIENT_ID` and `SECRET_CLIENT_SECRET`) at startup.
- Initial refresh token bootstrapped manually (see §8), stored in Secret Manager (`SECRET_TOKENS`).
- On every request: in-memory access token if valid, else refresh from Intuit.
- **Critical:** when Intuit rotates the refresh token, write the new value as a new Secret Manager version. Without this, the service breaks on next cold start.
- Concurrency on refresh: an `asyncio.Lock` (or process-wide threading lock) around the refresh path to avoid double-refresh under burst.

## 8. Bootstrap & re-auth (browser-driven, self-service)

qb-service owns the full OAuth flow via two admin-gated routes (see §6, §7). No CLI, no `gcloud`, no manual Secret Manager edits in the normal case.

### Routes

```
GET /admin/oauth/start
    → generates state token, redirects browser to Intuit consent screen
    → callback URL = https://<qb-service-domain>/admin/oauth/callback

GET /admin/oauth/callback?code=...&realmId=...&state=...
    → validates state, exchanges code for {access_token, refresh_token, realm_id}
    → writes refresh_token + realm_id to Secret Manager (new version)
    → returns a small success page
```

### First-time setup

1. Deploy qb-service to Cloud Run.
2. In the Intuit Developer console, register the callback URL: `https://<qb-service-domain>/admin/oauth/callback`.
3. Ensure your Google identity has `roles/run.invoker` on the admin routes.
4. Browse to `https://<qb-service-domain>/admin/oauth/start`, click through Intuit consent, done.

### Re-auth (scope change, expired refresh token, switching Intuit apps)

Same as first-time setup, step 4. The new refresh token lands as a new Secret Manager version; existing service instances pick it up on the next refresh cycle (or restart).

### Where the web app fits

The web app's "Connect QuickBooks" admin UI (if it has one) is just a link to `/admin/oauth/start`. The web app never sees an Intuit token, never holds the client secret, never touches Secret Manager. This is the boundary that keeps Option B clean.

### Why not put OAuth in the web app

It would force the web app to hold the Intuit client secret, implement state validation, handle the callback, and either write to Secret Manager or POST tokens to qb-service. All of that duplicates logic the qb-service already needs to own for the refresh-rotation case (§7). Centralizing keeps one place responsible for everything Intuit-OAuth-shaped.

## 9. Local dev

- `uv run uvicorn qbsvc.main:app --reload` for the server.
- Token storage in dev = a file-backed store reading from `~/.config/qb/` (reuse the CLI's keyring/file fallback so you don't need GCP locally).
- Set `QBSVC_TOKEN_BACKEND=file` for local, `secret_manager` in deployed envs.
- No need to mock QBO — point at Intuit's sandbox realm via env var override.

## 10. Observability

- Structured JSON logs to stdout (Cloud Run captures automatically).
- Every QBO call logged with: method, endpoint, status, duration, retry count, realm.
- Request ID propagated from web app (`X-Request-ID`) or generated; included in all log lines and error responses.
- Cloud Run built-in metrics (latency, error rate) sufficient for v1. No Prometheus, no traces yet.

## 11. Rate limiting

QBO limit: **500 req/min per realm**. With one realm and one consumer, unlikely to hit, but:

- Existing 429 retry (single retry with `Retry-After`) ports forward.
- Add a token-bucket limiter in front of QBO calls (process-local) to fail fast at 480/min rather than letting QBO 429 us under burst.
- No per-caller rate limiting (single trusted caller).

## 12. Idempotency

- **Reads:** trivially idempotent.
- **Invoice create:** Intuit enforces `DocNumber` uniqueness, which gives natural deduplication — duplicate POST returns 6240 ("Duplicate Document Number"). We translate to HTTP 409. Web app retries are safe because the lab number is deterministic per job.
- **Invoice line append:** not idempotent at the HTTP layer (two retries = two lines). Acceptable for v1 because the web app's create path is synchronous and human-driven; revisit only if we add async queues.
- **Updates (item, invoice, line):** *(Amendment 1)* QBO's `SyncToken` gives optimistic concurrency — a stale token returns QBO error 5010 ("Stale Object"), which we translate to HTTP 409. Safe to retry: re-read, re-apply.
- **Deletes:** *(Amendment 1)* idempotent in effect — deleting an already-deleted invoice (or deactivating an already-inactive item) surfaces as 404/409 from QBO; we pass through rather than masking.
- **Line update/delete:** *(Amendment 1)* read-modify-write on the parent invoice, so there's a race window between the read and the write. The stale-SyncToken 409 closes it; acceptable with a single human-driven consumer.

## 13. Testing

- `httpx.MockTransport` for unit tests against the QBO client.
- Integration tests against Intuit's sandbox, gated behind an env flag (don't run on every push).
- No live-QBO tests in CI.

## 14. Deployment

- Cloud Run service, region `us-central1` (or wherever the operator infra lives).
- Service account: dedicated, with `roles/secretmanager.secretAccessor` on the two secrets only.
- Min instances: 0 (cost) or 1 (no cold-start latency for refresh) — decide based on usage pattern.
- Concurrency: 80 (default) is fine; QBO calls are I/O bound.
- Deploy via `gcloud run deploy` initially; promote to GitHub Actions once stable.

### Companion public pages service *(Amendment 2)*

Intuit production keys require publicly resolvable URLs for the app's landing page, EULA, and privacy policy. qb-service is IAM-locked at the edge (`--no-allow-unauthenticated`), so those pages cannot live on it. Instead, a second, minimal Cloud Run service (`qb-pages`, working name) hosts them:

- Static HTML only (`/`, `/eula`, `/privacy`) — no QBO access, no secrets, no service account permissions, no network path to qb-service.
- Lives in this repo under `web/`; its own Dockerfile (static file server) and deploy config under `deploy/`.
- Deployed `--allow-unauthenticated`. Its only job is satisfying Intuit's app-deployment constraints; it is not part of the API surface.

## 15. Phased delivery

| Phase | Scope | Exit criteria |
|---|---|---|
| **0 — Bootstrap** | Fork repo, strip CLI-only code, FastAPI scaffold, `/healthz` | Local server runs, returns 200 |
| **1 — Token plumbing** | `TokenStore` protocol, file + Secret Manager backends, concurrent-safe refresh, `/admin/oauth/*` routes | Can complete OAuth via browser and do `GET /v1/customers` end-to-end against Cloud Run |
| **2 — Read endpoints** | `customers`, `items`, `invoices` GET routes + pagination + filters | Web app team can prototype against deployed service |
| **3 — Write endpoints** | `POST /v1/invoices`, `POST /v1/invoices/{id}/lines` | Test invoice round-trips through sandbox + prod realm |
| **3b — Extended writes** *(Amendment 1)* | `POST/PUT/DELETE /v1/items/*`, `PUT/DELETE /v1/invoices/{id}`, `POST /v1/invoices/{id}/void`, `PUT/DELETE /v1/invoices/{id}/lines/{line_id}` | Item lifecycle + invoice edit/void/delete round-trip through sandbox |
| **4 — Hardening** | Structured logging, error envelope, rate limiter, IAM lockdown, observability | Ready for consuming web app to depend on it |
| **4b — Production launch** *(Amendment 2)* | `qb-pages` public pages service; deploy both services to Cloud Run; Intuit app production keys + redirect URIs; OAuth bootstrap against the prod realm | `GET /v1/customers` works end-to-end against the production the operator realm |
| **5 — Web app integration** | (Other repo) | Phase 3 of the consuming app diagram works end-to-end |

Estimate: Phases 0–4 are roughly a week of focused work for one person, gated on Cloud Run + Secret Manager being set up.

## 16. Open questions

1. **Single realm or design for N?** Recommendation: single realm for v1 (simpler env config). Multi-realm becomes a `X-QB-Realm` header + per-realm token secret if the operator ever adds a second company.
2. **Pagination shape:** pass through QBO's `STARTPOSITION` offsets, or normalize to opaque cursors? Recommendation: opaque cursors (base64-encoded offset) so we can change the underlying impl later without breaking the web app.
3. **`modified_since` filter:** QBO supports `MetaData.LastUpdatedTime > 'YYYY-MM-DD'` via SQL. Worth exposing on all list endpoints so the web app can do delta pulls if it ever wants to cache. Cheap to add.
4. ~~Should the bootstrap refresh-token step live in this repo or stay in `quickbooks-cli`?~~ **Resolved:** qb-service owns the full OAuth flow via `/admin/oauth/*` (§8). No bootstrap CLI; the CLI keeps its own independent auth for its own use case.
5. **Schema validation strictness on writes:** reject unknown fields (Pydantic `extra="forbid"`) or pass through? Recommendation: forbid, so the web app gets clear errors instead of silent drops.
6. ~~**Versioning:** prefix routes with `/v1/`?~~ **Resolved:** yes — data routes are prefixed with `/v1/` (§6). Operational routes (`/healthz`, `/readyz`, `/admin/oauth/*`) stay unversioned.
7. ~~**Invoice delete vs. void:** does the web app need void, delete, or both?~~ **Resolved (Amendment 1):** expose both — `DELETE /v1/invoices/{id}` (hard delete) and `POST /v1/invoices/{id}/void`. Which one the web app actually uses is its call; dropping the unused route later is cheap.

## 17. What we are *not* deciding now

- The web app's own architecture (framework, hosting, DB).
- How the consuming app database schema maps Test Code → QBO Item Id (that mapping is owned by the web app, surfaced when it POSTs invoice lines).
- Drive folder provisioning. Out of scope for qb-service entirely — that's a separate concern (Google Drive API), not QBO.

---

## Amendment log

### Amendment 1 — 2026-06-06: full CRUD surface

The consuming app requirements list asks for a wider write surface than v1 scoped: item create/update/delete, invoice update/delete, and line-item update/delete. This amendment adds those routes (§6), a delivery phase for them (§15, Phase 3b), idempotency notes (§12), and one new open question (§16 #7).

**No OAuth change needed.** Intuit has no per-entity scopes; `com.intuit.quickbooks.accounting` already grants full read/write to every entity here. Entity/verb granularity is enforced entirely by which routes this service exposes.

QBO constraints that shaped the route semantics:

- **Items cannot be hard-deleted.** The API only supports `Active: false`. `DELETE /v1/items/{id}` is therefore a deactivation; QBO appends "(deleted)" to the name of deactivated items it must disambiguate.
- **Invoice delete removes the transaction.** QBO `operation=void` is the audit-friendly alternative (record survives at $0). We expose both — `DELETE /v1/invoices/{id}` and `POST /v1/invoices/{id}/void` — and let the web app pick; the unused route is cheap to drop later (§16 #7).
- **No per-line API.** QBO has no line-item endpoints; updating or removing a line means a full update of the parent invoice (read current state, modify the `Line[]` array, write back with the fresh `SyncToken`). The `/v1/invoices/{id}/lines/{line_id}` routes wrap that read-modify-write so the web app doesn't reimplement QBO's full-update semantics. `line_id` is QBO's stable `Line.Id` within the invoice.

### Amendment 2 — 2026-06-06: public pages service + production launch phase

Intuit requires a publicly reachable landing page, EULA URL, and privacy policy URL before issuing production keys for a developer app. qb-service rejects unauthenticated requests at the Cloud Run edge by design (§7), so it cannot host those pages itself. Options considered:

- ~~Open qb-service to `allUsers` and gate in-app~~ — contradicts the §7 edge-IAM design.
- ~~Static host outside GCP (e.g. GitHub Pages)~~ — viable, but splits deploy surface across providers.
- **Chosen:** a second, minimal public Cloud Run service (`qb-pages`) serving only static pages (§14). No secrets, no QBO access; blast radius of "public" is three HTML files.

Also adds Phase 4b (§15) covering the launch sequence: pages service → deploy both services → Intuit production keys → OAuth bootstrap against the prod realm.

### Amendment 3 — 2026-08-04: invoice custom-field pass-through

Invoices can carry QBO transaction custom fields (e.g. "P.O. Number", "Sales Rep"). These are **realm-configured**: their `DefinitionId`s and labels differ per QBO environment, and QBO exposes no first-class field for them — they live in the transaction's `CustomField[]` array. Rather than hard-code specific fields, `POST /v1/invoices` (and, via the shared body model, `PUT /v1/invoices/{id}`) accept an optional `custom_fields[]` of `{definition_id, value}` pairs and forward them verbatim as QBO `CustomField` entries.

- **The caller owns the `DefinitionId` map.** Keeping realm-specific ids out of the service leaves it environment-agnostic; the single consuming app already knows which realm it targets.
- **Writes that set custom fields add `include=enhancedAllCustomFields`.** Intuit requires this query param on invoice writes carrying custom fields; without it QBO can silently drop them in realms with the enhanced custom-field feature enabled. The service sends it on create and full-replace only when `custom_fields` is present, so ordinary invoices keep a minimal request.
- **Only `StringType` is API-writable**, so `value` is always a string. It's capped at 31 chars (QBO's `StringValue` limit) and `definition_id` is pinned to digits — both reject at the edge with a 422 rather than a downstream 502.
- **No change to the sparse flows.** Line append/update/delete send sparse updates that omit `CustomField`, so QBO preserves existing values. GET/list already return the raw `CustomField` array. Full-replace (`PUT`) reuses the create body, so it carries custom fields too.
- **Not queryable.** QBO's query language can't filter on `CustomField`, so there is no list filter for custom-field values.

A future amendment could add a discovery endpoint (`GET /v1/invoices/custom-fields`) so callers resolve names → `DefinitionId`s dynamically instead of holding the map, but that depends on QBO's awkward `Preferences.SalesFormsPrefs` source and is deferred until a consumer needs it.

## Review checklist

Before cloning to a new repo, confirm:

- [ ] Single realm assumption is correct (one QBO company for the operator)
- [ ] Cloud Run is the right runtime (vs. Cloud Functions Gen 2 or App Engine)
- [ ] Pagination as opaque cursors is acceptable
- [ ] Browser-driven OAuth via `/admin/oauth/*` is the right bootstrap approach
- [ ] Path-level IAM (admin vs. service-account callers) is achievable on Cloud Run, or split into two services if not
- [ ] Phased delivery order matches your priorities
- [ ] Any endpoints missing from §6 that Phase 3/4 of the consuming app diagram will need
