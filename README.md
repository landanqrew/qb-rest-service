# qb-service

Thin REST proxy in front of the QuickBooks Online API. Built for the Martin Water Labs Lab Intake web app — owns OAuth, token refresh, and rate-limit handling so consumers don't have to.

Architecture decision and design rationale: [`docs/qb-service-scope.md`](docs/qb-service-scope.md).

## Status

Phase 0 — bootstrap scaffold. `/healthz` works. No QBO routes wired up yet.

## Local development

```bash
uv sync
uv run uvicorn qbsvc.main:app --reload --port 8080
```

Then:

```bash
curl http://localhost:8080/healthz
# {"status":"ok"}
```

> **Deployed (Cloud Run)?** Smoke-check with `/readyz`, not `/healthz`. Google's
> frontend intercepts the literal `/healthz` path on `run.app` domains and 404s
> it before the container is reached. `/healthz` still works locally and for the
> container-internal startup probe; external checks must use `/readyz`.

## Layout

```
src/qbsvc/
├── main.py          # FastAPI app
├── config.py        # env-driven settings
├── exceptions.py    # APIError, AuthError, RateLimitError
├── api/             # QBO REST client (ported from quickbooks-cli)
├── auth/            # OAuth flow + token storage (Phase 1 refactor pending)
└── routes/
    └── health.py    # /healthz, /readyz
```

## Public pages companion (`qb-pages`)

`qb-service` is IAM-locked, so it can't host the public landing / EULA / privacy
URLs Intuit requires for production-app review. Those live in a separate, minimal
Cloud Run service, `qb-pages`, whose entire surface is three static HTML files
(`web/`). It has no QBO access, no secrets, and a permission-less service
account — "public" only ever means three HTML pages.

Deploy it with:

```bash
export GCP_PROJECT=your-project-id
./deploy/qb-pages.deploy.sh
```

See [`deploy/qb-pages-setup.md`](deploy/qb-pages-setup.md) for the full setup
(permission-less service account, Intuit URL registration, local `docker run`
verification). The EULA and privacy drafts are boilerplate marked **DRAFT** and
need human review before Intuit submission.

## Browser OAuth bootstrap companion (`qb-admin`)

`qb-service` is IAM-locked, so a plain browser (which can't attach a Cloud Run
identity token) can't run the Intuit OAuth handshake against it — which means a
consuming app can't offer a real **Connect QuickBooks** button. `qb-admin`
(issue #51) is the **same image** deployed as a second, *public* Cloud Run
service that serves only `/admin/oauth/*` (data routes off). It's public at the
edge but gated by a signed **launch token** the consuming app mints, so
non-admins can't initiate the flow. Both services share the `mwl-qb-tokens`
secret, so a connect done here is immediately live for `qb-service`.

```bash
export GCP_PROJECT=your-project-id REALM_ID=<realm> \
       RETURN_URL=https://your-app.example.com/settings/integrations
./deploy/qb-admin.deploy.sh
```

Three distinct surfaces, not to be confused: **readiness** (`GET
{qb-service}/readyz`, server-to-server IAM), the **data API** (`{qb-service}/v1/*`,
server-to-server IAM), and the **browser OAuth bootstrap**
(`{qb-admin}/admin/oauth/start?launch=…`, app-layer launch token). See
[`deploy/qb-admin-setup.md`](deploy/qb-admin-setup.md) for the full flow and the
consuming-app button integration.

## Related projects

- [`quickbooks-cli`](../quickbooks-cli) — the laptop CLI this service was forked from. Different use case, independent auth.
