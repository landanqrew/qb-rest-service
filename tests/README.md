# Tests

The suite is organized by **test kind**, so you can run a layer in isolation:

| Dir | Kind | What it covers | External deps |
|-----|------|----------------|---------------|
| `tests/unit/` | Unit | Pure logic — query builder, cursors, rate limiter, error envelopes, OAuth state, token store, shared route helpers. No app stack. | none |
| `tests/integration/` | Integration (in-process) | The full FastAPI app via `TestClient`, with QBO faked at the HTTP boundary by `httpx.MockTransport`. Real routing, middleware, deps, and error mapping. | none |
| `tests/e2e/` | End-to-end (local) | The `qb-pages` companion served by the **real nginx config** in a subprocess. | `nginx` binary (skips if absent) |
| `tests/live/` | Live smoke | Read-only checks against the **deployed sandbox** service — Cloud Run IAM, GFE path handling, live QBO responses. | deployed service + IAM token (skips unless configured) |

`tests/conftest.py` applies to every layer (it neutralizes a developer's local
`.env` so credentials never leak into tests).

## Running

```bash
# everything (live layer skips unless configured)
uv run --extra dev --extra gcp pytest

# one layer
uv run --extra dev --extra gcp pytest tests/unit
```

### Live smoke tests

Opt-in and strictly read-only (they never create/mutate/delete QBO data):

```bash
export QBSVC_LIVE_BASE_URL=https://qb-service-HASH-REGION.a.run.app
# token is read from QBSVC_LIVE_ID_TOKEN if set, else minted via gcloud:
export QBSVC_LIVE_ID_TOKEN="$(gcloud auth print-identity-token)"
uv run --extra dev --extra gcp pytest tests/live -q
```

The caller's identity must hold `roles/run.invoker` on the service. Without
`QBSVC_LIVE_BASE_URL` the whole layer skips.
