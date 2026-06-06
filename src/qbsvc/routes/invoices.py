from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qbsvc.api.client import QBClient
from qbsvc.deps import get_qb_client
from qbsvc.exceptions import APIError, AuthError, RateLimitError, TokenStoreError
from qbsvc.routes._common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    error_response,
    get_entity_detail,
    is_not_found,
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
    # QBO stores DocNumber as a string capped at 21 chars; rejecting longer
    # values here means callers see a 422 with the field name instead of an
    # opaque 502 from QBO downstream.
    doc_number: str = Field(min_length=1, max_length=21)
    txn_date: str | None = None
    lines: list[InvoiceLineCreate] = Field(default_factory=list)
    memo: str | None = None
    customer_memo: str | None = None

    @model_validator(mode="after")
    def _every_line_must_have_rate(self) -> "InvoiceCreate":
        # QBO requires `Amount` on every SalesItemLineDetail line and we
        # compute Amount from qty * rate; without a rate, the resulting body
        # would round-trip to QBO and come back as an opaque 502. Rate-less
        # lines belong on the append flow (#9), not the create body.
        if any(line.rate is None for line in self.lines):
            raise ValueError(
                "rate is required for every line on invoice create; "
                "use POST /invoices/{id}/lines to append a rate-less line"
            )
        return self


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
    # QBO requires Amount on each line and stores it as decimal(12,2); round
    # at the boundary so float artefacts like `3.0 * 0.10 = 0.30000000000000004`
    # never reach Intuit. `InvoiceCreate._every_line_must_have_rate` already
    # guarantees `rate` is set when a line is present on create, but we keep
    # the None branch for defence-in-depth.
    if line.rate is not None:
        qbo_line["Amount"] = round(line.qty * line.rate, 2)
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


# `status_code=201` on the decorator drives the OpenAPI schema; the explicit
# `JSONResponse(status_code=201, ...)` below is what actually sets the HTTP
# status at runtime. Both are needed — see FastAPI #5290.
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


# Intuit's "Stale Object Error" fault code, raised on sparse updates whose
# SyncToken doesn't match the server's current value. Surfaced as HTTP 409
# (QBO_STALE_SYNC_TOKEN) so the caller can re-fetch the invoice and retry
# the append against the new SyncToken — see scope §6.
_STALE_SYNC_TOKEN_CODE = "5010"


class InvoiceLineAppend(BaseModel):
    """Request body for `POST /v1/invoices/{id}/lines` — a single new line.

    `extra="forbid"` mirrors the create flow so typos on the web app side
    (e.g. `unit_price` instead of `rate`) error loudly instead of being
    silently dropped and producing a wrong invoice total.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    qty: float
    rate: float | None = None
    description: str | None = None


def _strip_subtotal_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # QBO appends a `SubTotalLineDetail` row to every invoice it returns. If
    # we POST it back inside a sparse update, QBO rejects the payload because
    # the subtotal is server-managed. Drop it before re-sending — QBO
    # regenerates it on the response.
    return [line for line in lines if line.get("DetailType") != "SubTotalLineDetail"]


def _is_stale_sync_token(exc: APIError) -> bool:
    if exc.status_code != 400 or not isinstance(exc.raw, dict):
        return False
    fault = exc.raw.get("Fault") or {}
    for error in fault.get("Error", []):
        if str(error.get("code", "")) == _STALE_SYNC_TOKEN_CODE:
            return True
    return False


def _load_invoice_for_write(
    client: QBClient, invoice_id: str
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Read the current invoice so a write can reuse its SyncToken.

    Every invoice write (line append/update/delete, full replace, delete,
    void) is a read-then-write against QBO's optimistic-concurrency model, so
    they all share this GET phase. Returns `(invoice, None)` on success or
    `(None, error_response)` with the failure mapped to the shared
    conventions: missing invoice → 404, auth/token-store → 503, rate limit →
    429, everything else → 502. The returned invoice is guaranteed to carry
    both `Id` and `SyncToken`; a 200 with either missing (a QBO beta-API
    quirk) is surfaced as a clean 502 rather than a downstream KeyError.
    """
    try:
        resp = client.get(f"invoice/{invoice_id}")
    except AuthError as exc:
        return None, error_response(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return None, error_response("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return None, error_response("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        if is_not_found(exc):
            return None, error_response(
                "NOT_FOUND",
                f"Invoice {invoice_id} not found",
                404,
                qbo_detail=exc.detail,
            )
        return None, error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    invoice = resp.get("Invoice")
    if not isinstance(invoice, dict):
        # The GET already succeeded (HTTP 200), so a missing or malformed
        # Invoice body (absent key, or a non-object like `{"Invoice": []}`) is
        # an upstream contract break, not proof the record is gone — QBO
        # signals "not found" with a 400/404 fault that is_not_found() catches
        # above. Surface it as 502 rather than a misleading 404 or an
        # unhandled 500 from dereferencing a non-dict.
        return None, error_response(
            "QBO_ERROR",
            "QBO did not return a valid Invoice object in the read response",
            502,
        )
    if invoice.get("Id") is None or invoice.get("SyncToken") is None:
        return None, error_response(
            "QBO_ERROR",
            "QBO returned an invoice with a missing Id or SyncToken",
            502,
        )
    return invoice, None


def _execute_invoice_write(
    client: QBClient,
    body: dict[str, Any],
    *,
    invoice_id: str,
    operation: str | None = None,
) -> JSONResponse:
    """POST the write phase of an invoice mutation and wrap the result.

    Shared by the append / line-edit / full-replace / delete / void handlers
    so the QBO-fault → HTTP mapping stays uniform: stale SyncToken (5010) →
    409, missing invoice → 404, auth/token-store → 503, rate limit → 429,
    everything else → 502. Returns the entity QBO echoes back (the updated
    invoice, or the delete confirmation stub) in the DetailResponse envelope.
    """
    params = {"operation": operation} if operation is not None else None
    try:
        resp = client.post("invoice", json_body=body, params=params)
    except AuthError as exc:
        return error_response(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return error_response("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return error_response("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        if _is_stale_sync_token(exc):
            return error_response(
                "QBO_STALE_SYNC_TOKEN",
                (
                    f"Invoice {invoice_id} was modified by another writer; "
                    "re-fetch and retry the write"
                ),
                409,
                qbo_detail=exc.detail,
            )
        if is_not_found(exc):
            return error_response(
                "NOT_FOUND",
                f"Invoice {invoice_id} not found",
                404,
                qbo_detail=exc.detail,
            )
        return error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    entity = resp.get("Invoice")
    if not isinstance(entity, dict):
        # Mirror the read-phase guard: a 200 whose Invoice body is missing or a
        # non-object (e.g. `{"Invoice": []}`) is an upstream contract break.
        # Surface 502 rather than crash in DetailResponse(data=entity) and
        # leak an unhandled 500.
        return error_response(
            "QBO_ERROR",
            "QBO did not return a valid Invoice object in the write response",
            502,
        )

    body_env = DetailResponse(data=entity)
    return JSONResponse(status_code=200, content=body_env.model_dump(exclude_none=True))


def _append_line_to_qbo(line: InvoiceLineAppend) -> dict[str, Any]:
    # Mirrors `_line_to_qbo` but tuned for the append flow: when `rate` is
    # omitted we deliberately leave both `UnitPrice` and `Amount` off the
    # line so QBO falls back to the item's stored default price and
    # computes Amount itself (acceptance criterion in #9).
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
    if line.rate is not None:
        qbo_line["Amount"] = round(line.qty * line.rate, 2)
    if line.description is not None:
        qbo_line["Description"] = line.description
    return qbo_line


# QBO Line.Id is a stable, server-assigned numeric string scoped to the
# invoice. Pin it to digits for the same reason as the entity ids: keep any
# quote / path meta-character out of the URL segment and the line lookup.
_LINE_ID_RE = re.compile(r"^\d{1,20}$")


def _write_invoice_lines(
    client: QBClient,
    invoice_id: str,
    invoice_id_val: str,
    sync_token_val: str,
    new_lines: list[dict[str, Any]],
) -> JSONResponse:
    """Sparse-update the invoice's `Line[]` and return the standard envelope.

    Shared by the append, line-update, and line-delete flows: each is a
    read-modify-write that hands QBO the full replacement `Line` array with a
    fresh SyncToken. The QBO-fault → HTTP mapping (notably stale SyncToken →
    409) lives in `_execute_invoice_write`.
    """
    sparse_body: dict[str, Any] = {
        "Id": invoice_id_val,
        "SyncToken": sync_token_val,
        "sparse": True,
        "Line": new_lines,
    }
    return _execute_invoice_write(client, sparse_body, invoice_id=invoice_id)


@router.post("/{invoice_id}/lines")
def append_invoice_line(
    invoice_id: str,
    payload: InvoiceLineAppend,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    if not _INVOICE_ID_RE.fullmatch(invoice_id):
        return error_response(
            "INVALID_PARAM",
            "invoice_id must be a numeric QBO entity id",
            400,
        )

    # Sparse update needs the current SyncToken and the existing Line array
    # — QBO replaces Line wholesale when sparse=true names it, so we read,
    # mutate, and write back in one round-trip.
    invoice, err = _load_invoice_for_write(client, invoice_id)
    if err is not None:
        return err
    assert invoice is not None  # narrow for the type checker

    existing_lines = _strip_subtotal_lines(invoice.get("Line") or [])
    new_lines = [*existing_lines, _append_line_to_qbo(payload)]
    return _write_invoice_lines(
        client, invoice_id, invoice["Id"], invoice["SyncToken"], new_lines
    )


@router.put("/{invoice_id}")
def replace_invoice(
    invoice_id: str,
    payload: InvoiceCreate,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    """Full replace of an invoice (QBO full update).

    Body mirrors `InvoiceCreate` — strict (`extra="forbid"`) and every line
    must carry a rate. We read the current invoice for its SyncToken, then
    POST the new state without `sparse` so QBO replaces the record wholesale
    rather than merging fields. This replaces the `Line` array too: callers
    wanting additive line changes should use `POST /invoices/{id}/lines`.

    Because this is a replace and not a merge, the `Line` key is always sent
    — including an explicit empty array when `lines` is empty — so the update
    is unambiguous. (Contrast `create`, which omits `Line` on empty input
    because QBO rejects an empty `Line` array on insert; a full update with
    an omitted `Line` would instead risk silently preserving the existing
    lines. If QBO rejects clearing the lines, its fault surfaces as a 502.)
    """
    if not _INVOICE_ID_RE.fullmatch(invoice_id):
        return error_response(
            "INVALID_PARAM",
            "invoice_id must be a numeric QBO entity id",
            400,
        )

    invoice, err = _load_invoice_for_write(client, invoice_id)
    if err is not None:
        return err
    assert invoice is not None  # narrow for the type checker

    # Full update == the create body plus Id + SyncToken and no `sparse`
    # flag. QBO interprets the absence of `sparse` as "replace every field".
    qbo_body = _payload_to_qbo_body(payload)
    # Force an explicit Line on the replace: _payload_to_qbo_body omits it
    # when there are no lines, but a full update must state the lines so the
    # replace is unambiguous rather than a silent preserve.
    qbo_body.setdefault("Line", [])
    qbo_body["Id"] = invoice["Id"]
    qbo_body["SyncToken"] = invoice["SyncToken"]
    return _execute_invoice_write(client, qbo_body, invoice_id=invoice_id)


@router.delete("/{invoice_id}")
def delete_invoice(
    invoice_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    """Hard-delete an invoice via QBO `operation=delete`.

    Deleting an already-deleted invoice surfaces QBO's own error (the GET
    phase returns 404) rather than masking it. Returns QBO's delete
    confirmation stub in the standard envelope.
    """
    if not _INVOICE_ID_RE.fullmatch(invoice_id):
        return error_response(
            "INVALID_PARAM",
            "invoice_id must be a numeric QBO entity id",
            400,
        )

    invoice, err = _load_invoice_for_write(client, invoice_id)
    if err is not None:
        return err
    assert invoice is not None  # narrow for the type checker

    delete_body = {"Id": invoice["Id"], "SyncToken": invoice["SyncToken"]}
    return _execute_invoice_write(
        client, delete_body, invoice_id=invoice_id, operation="delete"
    )


@router.post("/{invoice_id}/void")
def void_invoice(
    invoice_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    """Void an invoice via QBO `operation=void`.

    Same read-then-write mechanics as delete, but the record survives at $0
    instead of being removed — the audit-friendly alternative (scope §16 #7).
    Returns the voided invoice in the standard envelope.
    """
    if not _INVOICE_ID_RE.fullmatch(invoice_id):
        return error_response(
            "INVALID_PARAM",
            "invoice_id must be a numeric QBO entity id",
            400,
        )

    invoice, err = _load_invoice_for_write(client, invoice_id)
    if err is not None:
        return err
    assert invoice is not None  # narrow for the type checker

    void_body = {"Id": invoice["Id"], "SyncToken": invoice["SyncToken"]}
    return _execute_invoice_write(
        client, void_body, invoice_id=invoice_id, operation="void"
    )


def _validate_invoice_and_line_ids(
    invoice_id: str, line_id: str
) -> JSONResponse | None:
    """Shared id guard for the per-line routes. Returns an error envelope when
    either id is malformed, else None."""
    if not _INVOICE_ID_RE.fullmatch(invoice_id):
        return error_response(
            "INVALID_PARAM",
            "invoice_id must be a numeric QBO entity id",
            400,
        )
    if not _LINE_ID_RE.fullmatch(line_id):
        return error_response(
            "INVALID_PARAM",
            "line_id must be a numeric QBO line id",
            400,
        )
    return None


def _find_line_index(lines: list[dict[str, Any]], line_id: str) -> int | None:
    """Index of the line whose `Line.Id` equals `line_id`, or None. QBO stores
    Id as a string but we compare stringwise to be robust to either shape.
    Lines without an Id (e.g. the SubTotal row) are skipped explicitly rather
    than coerced to the string "None", which would falsely match line_id="None"
    if this helper is ever reused without the `_LINE_ID_RE` digit guard."""
    for index, line in enumerate(lines):
        existing_id = line.get("Id")
        if existing_id is not None and str(existing_id) == line_id:
            return index
    return None


def _replace_line_to_qbo(line: InvoiceLineAppend, line_id: str) -> dict[str, Any]:
    # Same line shape as the append flow, but we re-attach the existing
    # Line.Id so QBO updates the row in place rather than dropping it and
    # minting a new one (which would renumber it and lose its identity).
    qbo_line = _append_line_to_qbo(line)
    qbo_line["Id"] = line_id
    return qbo_line


@router.put("/{invoice_id}/lines/{line_id}")
def update_invoice_line(
    invoice_id: str,
    line_id: str,
    payload: InvoiceLineAppend,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    """Replace a single line on an existing invoice.

    QBO has no per-line API, so this is a read-modify-write of the parent
    invoice: read the current Line array, swap the matching line in place,
    and sparse-update back with the fresh SyncToken (scope §6, Amendment 1).
    """
    invalid = _validate_invoice_and_line_ids(invoice_id, line_id)
    if invalid is not None:
        return invalid

    invoice, err = _load_invoice_for_write(client, invoice_id)
    if err is not None:
        return err
    assert invoice is not None  # narrow for the type checker

    existing_lines = _strip_subtotal_lines(invoice.get("Line") or [])
    index = _find_line_index(existing_lines, line_id)
    if index is None:
        return error_response(
            "NOT_FOUND",
            f"Line {line_id} not found on invoice {invoice_id}",
            404,
        )

    new_lines = list(existing_lines)
    new_lines[index] = _replace_line_to_qbo(payload, line_id)
    return _write_invoice_lines(
        client, invoice_id, invoice["Id"], invoice["SyncToken"], new_lines
    )


@router.delete("/{invoice_id}/lines/{line_id}")
def delete_invoice_line(
    invoice_id: str,
    line_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    """Remove a single line from an existing invoice via the same
    read-modify-write as the update flow, minus the replacement."""
    invalid = _validate_invoice_and_line_ids(invoice_id, line_id)
    if invalid is not None:
        return invalid

    invoice, err = _load_invoice_for_write(client, invoice_id)
    if err is not None:
        return err
    assert invoice is not None  # narrow for the type checker

    existing_lines = _strip_subtotal_lines(invoice.get("Line") or [])
    index = _find_line_index(existing_lines, line_id)
    if index is None:
        return error_response(
            "NOT_FOUND",
            f"Line {line_id} not found on invoice {invoice_id}",
            404,
        )

    new_lines = [line for i, line in enumerate(existing_lines) if i != index]
    if not new_lines:
        # QBO rejects an invoice with an empty Line array (it comes back as an
        # opaque 5xx we'd pass through as 502). Catch the invariant up front and
        # tell the caller to delete the invoice instead — see issue acceptance.
        return error_response(
            "CANNOT_DELETE_LAST_LINE",
            (
                f"Line {line_id} is the only line on invoice {invoice_id}; "
                "an invoice must keep at least one line. Delete the invoice "
                "instead of its last line."
            ),
            409,
        )
    return _write_invoice_lines(
        client, invoice_id, invoice["Id"], invoice["SyncToken"], new_lines
    )
