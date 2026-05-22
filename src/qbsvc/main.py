from __future__ import annotations

from fastapi import FastAPI

from qbsvc.routes import admin_oauth, customers, health


def create_app() -> FastAPI:
    app = FastAPI(
        title="qb-service",
        description="Thin REST proxy in front of QuickBooks Online for the Martin Water Labs Lab Intake app.",
        version="0.1.0",
    )
    app.include_router(health.router)
    app.include_router(admin_oauth.router)
    app.include_router(customers.router)
    return app


app = create_app()
