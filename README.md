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

## Related projects

- [`quickbooks-cli`](../quickbooks-cli) — the laptop CLI this service was forked from. Different use case, independent auth.
