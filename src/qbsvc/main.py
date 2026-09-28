from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from qbsvc import deps
from qbsvc.api.client import QBClient
from qbsvc.auth.admin_gate import AdminGateMiddleware, ensure_admin_gate_configured
from qbsvc.config import Settings, get_settings
from qbsvc.errors import register_exception_handlers
from qbsvc.logging import configure_logging
from qbsvc.middleware import RequestIDMiddleware
from qbsvc.routes import admin_oauth, customers, health, invoices, items


_log = logging.getLogger("qbsvc.startup")


def _warm_token_path(settings: Settings) -> None:
    """Build the Secret Manager token store and load (refreshing if expired)
    the QBO token before serving.

    On a cold instance the first requests otherwise pay for the Secret Manager
    import, channel setup and token refresh themselves — 12-47s in prod, past
    callers' timeouts. Uvicorn binds the port only after lifespan startup, so
    Cloud Run's startup probe holds traffic until this finishes. Failures are
    logged, not raised: requests then take the same lazy path as before, and
    /readyz still reports the auth problem.
    """
    if not settings.enable_data_routes or settings.token_backend != "secret_manager":
        return
    try:
        client = QBClient(token_store=deps.get_token_store(settings=settings), settings=settings)
        try:
            client.ensure_ready()
        finally:
            client.close()
    except Exception:
        _log.warning("startup_token_warmup_failed", exc_info=True)
    else:
        _log.info("startup_token_warmup_ok")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await asyncio.to_thread(_warm_token_path, get_settings())
    yield


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    # Refuse to start on Cloud Run with the /admin/* gate silently disabled.
    ensure_admin_gate_configured(settings)

    app = FastAPI(
        title="qb-service",
        description="Thin REST proxy in front of QuickBooks Online for a consuming web app.",
        version="0.1.0",
        lifespan=_lifespan,
    )
    # Middleware stack (outermost → innermost):
    #   RequestIDMiddleware  → AdminGateMiddleware  → routes
    # Starlette runs middleware in reverse-add order, so the gate is added
    # first (inner) and RequestID is added last (outer). That way a 403 from
    # the gate flows back out through RequestID and picks up the X-Request-ID
    # header callers use to correlate the rejection in logs.
    app.add_middleware(AdminGateMiddleware)
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    # /healthz + /readyz are always served — Cloud Run's startup/readiness
    # probes need them on every deployment shape.
    app.include_router(health.router)

    # Route-group toggles (issue #51) let the same image deploy twice: an
    # IAM-locked data service and a public browser OAuth bootstrap service.
    # Both default on, so the single-service deployment and local dev are
    # unchanged.
    if settings.enable_admin_routes:
        # /admin/oauth/* stays unversioned — it's an admin surface, not data API.
        app.include_router(admin_oauth.router)
    if settings.enable_data_routes:
        # Data routes are /v1/-prefixed so a future shape change can ride
        # alongside /v1 without a breaking client change.
        app.include_router(customers.router, prefix="/v1")
        app.include_router(items.router, prefix="/v1")
        app.include_router(invoices.router, prefix="/v1")
    return app


app = create_app()
