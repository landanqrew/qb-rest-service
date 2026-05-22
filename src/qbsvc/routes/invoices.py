from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from qbsvc.api.client import QBClient
from qbsvc.deps import get_qb_client
from qbsvc.exceptions import APIError, AuthError, RateLimitError, TokenStoreError
from qbsvc.routes._common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    error_response,
    get_entity_detail,
    list_entity,
)
from qbsvc.schemas import DetailResponse

router = APIRouter(prefix="/invoices", tags=["invoices"])

# QBO entity IDs are numeric strings; pinning to digits keeps any quote or
# whitespace out of the interpolated SQL literal and out of the URL segment
# used by the detail endpoint.
_CUSTOMER_ID_RE = re.compile(r"^\d{1,20}$")
_INVOICE_ID_RE = re.compile(r"^\d{1,20}$")

# DocNumber is a free-form QBO string capped at 21 chars; the lab format is
# YY-MM-####. The allowlist (letters, digits, dash, underscore, dot, space)
# covers every shape we expect and excludes single quotes / SQL meta-chars
# so the value can be inlined into the WHERE clause safely. Callers strip
# the value before matching so padded query params can't silently zero-match.
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
        # Strip first so a padded query-string value (e.g. " 26-02-0071")
        # doesn't silently zero-match against QBO's unpadded stored value.
        doc_number = doc_number.strip()
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
    if not _INVOICE_ID_RE.fullmatch(invoice_id):
        return error_response(
            "INVALID_PARAM",
            "invoice_id must be a numeric QBO entity id",
            400,
        )
    return get_entity_detail(client, entity="Invoice", entity_id=invoice_id)


# Intuit's "Duplicate Document Number Exists" fault code. Surfaced as HTTP
# 409 so the Lab Intake app can dedupe on the deterministic lab number
# without retrying or guessing. See scope §12.
_DUPLICATE_DOC_NUMBER_CODE = "6240"


class InvoiceLineCreate(BaseModel):
    """One line on an invoice-create request.

    `extra="forbid"` is intentional: typos on the web app side (e.g.
    `unit_price` instead of `rate`) must error loudly rather than be
    silently dropped on the floor and produce a wrong invoice total.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    qty: float
    rate: float | None = None
    description: str | None = None


class InvoiceCreate(BaseModel):
    """Request body for `POST /v1/invoices`.

    Mirrors scope §6 — `customer_id` + `doc_number` are required, everything
    else is optional, and `lines` may be empty (Phase 3 of the Lab Intake
    flow creates the invoice empty, then appends lines via #9).
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str
    doc_number: str
    txn_date: str | None = None
    lines: list[InvoiceLineCreate] = Field(default_factory=list)
    memo: str | None = None
    customer_memo: str | None = None


def _line_to_qbo(line: InvoiceLineCreate) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "ItemRef": {"value": line.item_id},
        "Qty": line.qty,
    }
    if line.rate is not None:
        detail["UnitPrice"] = line.rate

    qbo_line: dict[str, Any] = {
        "DetailType": "SalesItemLineDetail",
        "SalesItemLineDetail": detail,
    }
    # QBO requires Amount on each line and does not compute it server-side;
    # only set it when we have a rate. When rate is absent the caller is
    # expected to follow up with the line-append flow (#9) which can look
    # the item's default rate up.
    if line.rate is not None:
        qbo_line["Amount"] = line.qty * line.rate
    if line.description is not None:
        qbo_line["Description"] = line.description
    return qbo_line


def _payload_to_qbo_body(payload: InvoiceCreate) -> dict[str, Any]:
    body: dict[str, Any] = {
        "CustomerRef": {"value": payload.customer_id},
        "DocNumber": payload.doc_number,
    }
    if payload.txn_date is not None:
        body["TxnDate"] = payload.txn_date
    if payload.memo is not None:
        body["PrivateNote"] = payload.memo
    if payload.customer_memo is not None:
        body["CustomerMemo"] = {"value": payload.customer_memo}
    if payload.lines:
        # An empty `lines` list intentionally omits Line entirely — QBO
        # rejects Invoice create with an empty Line array.
        body["Line"] = [_line_to_qbo(line) for line in payload.lines]
    return body


def _is_duplicate_doc_number(exc: APIError) -> bool:
    if exc.status_code != 400 or not isinstance(exc.raw, dict):
        return False
    fault = exc.raw.get("Fault") or {}
    for error in fault.get("Error", []):
        if str(error.get("code", "")) == _DUPLICATE_DOC_NUMBER_CODE:
            return True
    return False


@router.post("", status_code=201)
def create_invoice(
    payload: InvoiceCreate,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    qbo_body = _payload_to_qbo_body(payload)
    try:
        resp = client.post("invoice", json_body=qbo_body)
    except AuthError as exc:
        return error_response(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return error_response("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return error_response("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        if _is_duplicate_doc_number(exc):
            return error_response(
                "QBO_DUPLICATE_DOCNUMBER",
                f"DocNumber {payload.doc_number} already exists",
                409,
                qbo_detail=exc.detail,
            )
        return error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    invoice = resp.get("Invoice")
    if invoice is None:
        return error_response(
            "QBO_ERROR",
            "QBO did not return an Invoice in the create response",
            502,
        )

    body = DetailResponse(data=invoice)
    return JSONResponse(status_code=201, content=body.model_dump(exclude_none=True))
