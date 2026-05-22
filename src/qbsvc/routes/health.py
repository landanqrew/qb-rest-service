from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from qbsvc.api.client import QBClient
from qbsvc.deps import get_qb_client
from qbsvc.errors import envelope
from qbsvc.exceptions import AuthError, TokenStoreError

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(client: QBClient = Depends(get_qb_client)) -> JSONResponse:
    try:
        client.ensure_ready()
    except AuthError as exc:
        return envelope(exc.code, str(exc), status=503)
    except TokenStoreError as exc:
        # Intuit has already rotated the refresh token; the store could not
        # persist the new value. Surface loudly so the operator re-auths
        # before the next cold start dies on an invalidated token.
        return envelope("TOKEN_STORE_FAILED", str(exc), status=503)
    return JSONResponse(status_code=200, content={"status": "ok"})
