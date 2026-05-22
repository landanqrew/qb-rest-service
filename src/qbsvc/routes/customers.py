from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from qbsvc.api.client import QBClient
from qbsvc.api.cursors import decode_cursor, encode_cursor
from qbsvc.deps import get_qb_client
from qbsvc.exceptions import (
    APIError,
    AuthError,
    PaginationError,
    RateLimitError,
    TokenStoreError,
)
from qbsvc.schemas import (
    DetailResponse,
    ErrorPayload,
    ErrorResponse,
    ListResponse,
    Pagination,
)

router = APIRouter(prefix="/customers", tags=["customers"])

# QBO MAXRESULTS hard limit is 1000; 100 matches the existing pagination helper default.
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100

# Strict YYYY-MM-DD only. date.fromisoformat() in 3.11+ also accepts ordinal
# and basic-format dates which would make the SQL literal ambiguous and could
# admit injection vectors through the looser shapes; pin the wire format here.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_iso_date(value: str) -> date | None:
    if not _ISO_DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _err(
    code: str, message: str, status: int, qbo_detail: str | None = None
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorPayload(code=code, message=message, qbo_detail=qbo_detail)
    )
    return JSONResponse(
        status_code=status, content=payload.model_dump(exclude_none=True)
    )


def _is_not_found(exc: APIError) -> bool:
    # QBO surfaces missing entities as HTTP 400 with Fault.Error code 610
    # ("Object Not Found"); some endpoints also return a plain 404. Detect
    # both so callers see a clean 404 either way.
    if exc.status_code == 404:
        return True
    if exc.status_code == 400 and "object not found" in exc.detail.lower():
        return True
    return False


@router.get("")
def list_customers(
    client: Annotated[QBClient, Depends(get_qb_client)],
    active: Annotated[bool, Query()] = True,
    modified_since: Annotated[
        str | None,
        Query(description="ISO date, e.g. 2026-01-01"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> JSONResponse:
    try:
        start = decode_cursor(cursor) if cursor else 1
    except PaginationError as exc:
        return _err("INVALID_CURSOR", str(exc), 400)

    if modified_since is not None and _parse_iso_date(modified_since) is None:
        # Hard-fail anything that isn't strictly YYYY-MM-DD so the value can
        # never carry a quote or other SQL-meaningful character into the
        # query string below.
        return _err(
            "INVALID_PARAM",
            "modified_since must be a calendar date in YYYY-MM-DD format",
            400,
        )

    where_clauses: list[str] = []
    if active:
        where_clauses.append("Active = true")
    else:
        # QBO's default SELECT * filters to Active=true; explicit IN clause
        # is the documented way to surface inactive rows too.
        where_clauses.append("Active IN (true, false)")
    if modified_since:
        where_clauses.append(f"MetaData.LastUpdatedTime > '{modified_since}'")

    where = " AND ".join(where_clauses)

    # Fetch limit+1 to detect has_more without a second round-trip; trim
    # before returning so the page never exceeds the requested size.
    fetch_size = limit + 1
    sql = (
        f"SELECT * FROM Customer WHERE {where} "
        f"STARTPOSITION {start} MAXRESULTS {fetch_size}"
    )

    try:
        results = client.query(sql)
    except AuthError as exc:
        return _err(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return _err("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return _err("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        return _err("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    has_more = len(results) > limit
    page = results[:limit]
    next_cursor = encode_cursor(start + len(page)) if has_more else None

    body = ListResponse(
        data=page,
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more),
    )
    return JSONResponse(status_code=200, content=body.model_dump(exclude_none=True))


@router.get("/{customer_id}")
def get_customer(
    customer_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    try:
        resp = client.get(f"customer/{customer_id}")
    except AuthError as exc:
        return _err(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return _err("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return _err("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        if _is_not_found(exc):
            return _err(
                "NOT_FOUND",
                f"Customer {customer_id} not found",
                404,
                qbo_detail=exc.detail,
            )
        return _err("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    customer = resp.get("Customer")
    if not customer:
        return _err("NOT_FOUND", f"Customer {customer_id} not found", 404)

    body = DetailResponse(data=customer)
    return JSONResponse(status_code=200, content=body.model_dump(exclude_none=True))
