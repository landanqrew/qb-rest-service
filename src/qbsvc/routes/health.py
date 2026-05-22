from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from qbsvc.api.client import QBClient
from qbsvc.deps import get_qb_client
from qbsvc.exceptions import AuthError

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(client: QBClient = Depends(get_qb_client)) -> JSONResponse:
    try:
        client.ensure_ready()
    except AuthError as exc:
        message = str(exc)
        code = (
            "NOT_AUTHENTICATED"
            if "not authenticated" in message.lower()
            else "TOKEN_REFRESH_FAILED"
        )
        return JSONResponse(
            status_code=503,
            content={"error": {"code": code, "message": message}},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
