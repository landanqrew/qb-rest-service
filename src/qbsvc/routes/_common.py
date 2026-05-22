from __future__ import annotations

import re
from datetime import date

from fastapi.responses import JSONResponse

from qbsvc.api.client import QBClient
from qbsvc.api.cursors import decode_cursor, encode_cursor
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

# QBO MAXRESULTS hard limit is 1000; 100 matches the existing pagination helper default.
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100

# Strict YYYY-MM-DD only. date.fromisoformat() in 3.11+ also accepts ordinal
# and basic-format dates which would make the SQL literal ambiguous and could
# admit injection vectors through the looser shapes; pin the wire format here.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_iso_date(value: str) -> date | None:
    if not _ISO_DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def error_response(
    code: str, message: str, status: int, qbo_detail: str | None = None
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorPayload(code=code, message=message, qbo_detail=qbo_detail)
    )
    return JSONResponse(
        status_code=status, content=payload.model_dump(exclude_none=True)
    )


def is_not_found(exc: APIError) -> bool:
    # QBO surfaces missing entities as HTTP 400 with Fault.Error code 610
    # ("Object Not Found"); some endpoints also return a plain 404. Detect
    # both so callers see a clean 404 either way.
    if exc.status_code == 404:
        return True
    if exc.status_code == 400 and "object not found" in exc.detail.lower():
        return True
    return False


def list_entity(
    client: QBClient,
    *,
    entity: str,
    active: bool,
    modified_since: str | None,
    limit: int,
    cursor: str | None,
) -> JSONResponse:
    """Run a paginated QBO list query against `entity` and return the envelope.

    Shared between every Phase 2 read endpoint so the cursor/filter/envelope
    contract stays uniform across customers, items, invoices, etc.
    """
    try:
        start = decode_cursor(cursor) if cursor else 1
    except PaginationError as exc:
        return error_response("INVALID_CURSOR", str(exc), 400)

    if modified_since is not None and parse_iso_date(modified_since) is None:
        # Hard-fail anything that isn't strictly YYYY-MM-DD so the value can
        # never carry a quote or other SQL-meaningful character into the
        # query string below.
        return error_response(
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
        f"SELECT * FROM {entity} WHERE {where} "
        f"STARTPOSITION {start} MAXRESULTS {fetch_size}"
    )

    try:
        results = client.query(sql)
    except AuthError as exc:
        return error_response(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return error_response("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return error_response("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        return error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    has_more = len(results) > limit
    page = results[:limit]
    next_cursor = encode_cursor(start + len(page)) if has_more else None

    body = ListResponse(
        data=page,
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more),
    )
    return JSONResponse(status_code=200, content=body.model_dump(exclude_none=True))


def get_entity_detail(
    client: QBClient,
    *,
    entity: str,
    entity_id: str,
    label: str | None = None,
) -> JSONResponse:
    """Fetch a single QBO entity by id and wrap it in the detail envelope.

    `entity` is the QBO PascalCase entity name (e.g. "Customer", "Item").
    `label` is the user-facing name used in NOT_FOUND messages; defaults to
    `entity`.
    """
    label = label or entity
    endpoint_path = entity.lower()
    try:
        resp = client.get(f"{endpoint_path}/{entity_id}")
    except AuthError as exc:
        return error_response(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return error_response("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return error_response("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        if is_not_found(exc):
            return error_response(
                "NOT_FOUND",
                f"{label} {entity_id} not found",
                404,
                qbo_detail=exc.detail,
            )
        return error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    entity_body = resp.get(entity)
    if not entity_body:
        return error_response("NOT_FOUND", f"{label} {entity_id} not found", 404)

    body = DetailResponse(data=entity_body)
    return JSONResponse(status_code=200, content=body.model_dump(exclude_none=True))
