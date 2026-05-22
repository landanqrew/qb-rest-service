from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from qbsvc.api.client import QBClient
from qbsvc.deps import get_qb_client
from qbsvc.routes._common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    error_response,
    get_entity_detail,
    list_entity,
)

router = APIRouter(prefix="/invoices", tags=["invoices"])

# QBO entity IDs are numeric strings; pinning to digits keeps any quote or
# whitespace out of the interpolated SQL literal.
_CUSTOMER_ID_RE = re.compile(r"^\d{1,20}$")

# DocNumber is a free-form QBO string capped at 21 chars; the lab format is
# YY-MM-####. The allowlist (letters, digits, dash, underscore, dot, space)
# covers every shape we expect and excludes single quotes / SQL meta-chars
# so the value can be inlined into the WHERE clause safely.
_DOC_NUMBER_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,21}$")


@router.get("")
def list_invoices(
    client: Annotated[QBClient, Depends(get_qb_client)],
    customer_id: Annotated[str | None, Query()] = None,
    doc_number: Annotated[
        str | None,
        Query(description="Exact DocNumber match (no LIKE / prefix)"),
    ] = None,
    modified_since: Annotated[
        str | None,
        Query(description="ISO date, e.g. 2026-01-01"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> JSONResponse:
    filters: list[str] = []
    if customer_id is not None:
        if not _CUSTOMER_ID_RE.fullmatch(customer_id):
            return error_response(
                "INVALID_PARAM",
                "customer_id must be a numeric QBO entity id",
                400,
            )
        filters.append(f"CustomerRef = '{customer_id}'")
    if doc_number is not None:
        if not _DOC_NUMBER_RE.fullmatch(doc_number):
            return error_response(
                "INVALID_PARAM",
                "doc_number must be 1-21 chars of letters, digits, dash, underscore, dot, or space",
                400,
            )
        filters.append(f"DocNumber = '{doc_number}'")

    return list_entity(
        client,
        entity="Invoice",
        extra_filters=filters,
        modified_since=modified_since,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{invoice_id}")
def get_invoice(
    invoice_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    return get_entity_detail(client, entity="Invoice", entity_id=invoice_id)
