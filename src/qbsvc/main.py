from __future__ import annotations

from fastapi import FastAPI

from qbsvc.errors import register_exception_handlers
from qbsvc.logging import configure_logging
from qbsvc.middleware import RequestIDMiddleware
from qbsvc.routes import admin_oauth, customers, health, invoices, items


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="qb-service",
        description="Thin REST proxy in front of QuickBooks Online for the Martin Water Labs Lab Intake app.",
        version="0.1.0",
    )
    # X-Request-ID first so every log line and error envelope downstream
    # carries the same correlation id.
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(admin_oauth.router)
    # Data routes are /v1/-prefixed so a future shape change can ride alongside
    # /v1 without a breaking client change. /healthz, /readyz, /admin/oauth/*
    # stay unversioned — they're ops/admin surfaces, not data API.
    app.include_router(customers.router, prefix="/v1")
    app.include_router(items.router, prefix="/v1")
    app.include_router(invoices.router, prefix="/v1")
    return app


app = create_app()
