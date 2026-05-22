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
    extra_filters: list[str] | None = None,
    modified_since: str | None,
    limit: int,
    cursor: str | None,
) -> JSONResponse:
    """Run a paginated QBO list query against `entity` and return the envelope.

    Shared between every Phase 2 read endpoint so the cursor/filter/envelope
    contract stays uniform across customers, items, invoices, etc.

    `extra_filters` is a list of caller-built WHERE clauses (e.g.
    `["Active = true"]` or `["CustomerRef = '55'"]`). Callers are responsible
    for validating any user input that ends up inside these clauses; this
    helper trusts the strings it receives and interpolates them verbatim.
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

    where_clauses: list[str] = list(extra_filters or [])
    if modified_since:
        where_clauses.append(f"MetaData.LastUpdatedTime > '{modified_since}'")

    # Fetch limit+1 to detect has_more without a second round-trip; trim
    # before returning so the page never exceeds the requested size.
    # Clamped to QBO's documented 1000-row hard cap: at limit=MAX_PAGE_SIZE
    # the has_more probe slot is lost and a full 1000-row page reports
    # has_more=false even if a 1001st row exists. Callers needing exact
    # boundary detection should page with a smaller window.
    fetch_size = min(limit + 1, 1000)
    # Entities like Invoice have no Active column, so callers can pass no
    # filters at all — in that case skip WHERE entirely rather than emit
    # `WHERE  STARTPOSITION ...` which QBO rejects as a syntax error.
    where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = (
        f"SELECT * FROM {entity}{where_sql} "
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
    if entity_body is None:
        return error_response("NOT_FOUND", f"{label} {entity_id} not found", 404)

    body = DetailResponse(data=entity_body)
    return JSONResponse(status_code=200, content=body.model_dump(exclude_none=True))
