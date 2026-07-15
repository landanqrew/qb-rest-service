from __future__ import annotations

from fastapi import FastAPI

from qbsvc.auth.admin_gate import AdminGateMiddleware, ensure_admin_gate_configured
from qbsvc.config import get_settings
from qbsvc.errors import register_exception_handlers
from qbsvc.logging import configure_logging
from qbsvc.middleware import RequestIDMiddleware
from qbsvc.routes import admin_oauth, customers, health, invoices, items


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    # Refuse to start on Cloud Run with the /admin/* gate silently disabled.
    ensure_admin_gate_configured(settings)

    app = FastAPI(
        title="qb-service",
        description="Thin REST proxy in front of QuickBooks Online for a consuming web app.",
        version="0.1.0",
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
