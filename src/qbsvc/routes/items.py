from __future__ import annotations

import re
from typing import Annotated, Any, Literal

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

router = APIRouter(prefix="/items", tags=["items"])

# QBO entity IDs are numeric strings; pinning to digits keeps any quote or
# whitespace out of the URL segment used by the detail/update endpoints.
_ITEM_ID_RE = re.compile(r"^\d{1,20}$")

# QBO item Type enum. The Lab Intake catalogue is Services today, but Inventory
# / NonInventory round-trip the same create call (Inventory needs extra QBO
# fields we don't expose yet — those surface as a 502 from QBO, not a silent
# success).
ItemType = Literal["Service", "Inventory", "NonInventory"]


@router.get("")
def list_items(
    client: Annotated[QBClient, Depends(get_qb_client)],
    active: Annotated[bool, Query()] = True,
    modified_since: Annotated[
        str | None,
        Query(description="ISO date, e.g. 2026-01-01"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> JSONResponse:
    active_clause = "Active = true" if active else "Active IN (true, false)"
    return list_entity(
        client,
        entity="Item",
        extra_filters=[active_clause],
        modified_since=modified_since,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{item_id}")
def get_item(
    item_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    if not _ITEM_ID_RE.fullmatch(item_id):
        return error_response(
            "INVALID_PARAM",
            "item_id must be a numeric QBO entity id",
            400,
        )
    return get_entity_detail(client, entity="Item", entity_id=item_id)


# Intuit's "Duplicate Name Exists Error" fault code. QBO enforces item-name
# uniqueness; we surface the collision as HTTP 409 (QBO_DUPLICATE_NAME) so the
# Lab Intake app can dedupe deterministically — the same pattern the invoice
# routes use for duplicate DocNumber (also code 6240). See scope §12.
_DUPLICATE_NAME_CODE = "6240"

# Intuit's "Stale Object Error" fault code, raised on sparse updates whose
# SyncToken doesn't match the server's current value. Surfaced as HTTP 409
# (QBO_STALE_SYNC_TOKEN) so the caller can re-fetch the item and retry against
# the new SyncToken — see scope §6.
_STALE_SYNC_TOKEN_CODE = "5010"


def _has_fault_code(exc: APIError, code: str) -> bool:
    if exc.status_code != 400 or not isinstance(exc.raw, dict):
        return False
    fault = exc.raw.get("Fault") or {}
    for error in fault.get("Error", []):
        if str(error.get("code", "")) == code:
            return True
    return False


class ItemCreate(BaseModel):
    """Request body for `POST /v1/items` (scope §6).

    `extra="forbid"` is intentional: typos on the web app side (e.g.
    `unitprice` for `unit_price`) must error loudly rather than be silently
    dropped and produce an item with the wrong price.
    """

    model_config = ConfigDict(extra="forbid")

    # QBO caps Item.Name at 100 chars; rejecting longer values here means
    # callers see a 422 with the field name instead of an opaque 502 from QBO.
    name: str = Field(min_length=1, max_length=100)
    type: ItemType
    # min_length=1 so an empty id fails as a clean 422 here instead of
    # round-tripping to QBO as IncomeAccountRef:{"value": ""} and coming back
    # as an opaque 502.
    income_account_id: str = Field(min_length=1)
    unit_price: float | None = None


class ItemUpdate(BaseModel):
    """Request body for `PUT /v1/items/{id}` — a SyncToken-aware sparse update.

    Every field is optional; only the ones provided are written. At least one
    must be present so an empty body short-circuits to 422 instead of burning
    a pointless QBO round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    type: ItemType | None = None
    income_account_id: str | None = Field(default=None, min_length=1)
    unit_price: float | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "ItemUpdate":
        if all(
            value is None
            for value in (
                self.name,
                self.type,
                self.income_account_id,
                self.unit_price,
            )
        ):
            raise ValueError("at least one field must be provided for a sparse update")
        return self


def _create_to_qbo_body(payload: ItemCreate) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Name": payload.name,
        "Type": payload.type,
        "IncomeAccountRef": {"value": payload.income_account_id},
    }
    if payload.unit_price is not None:
        body["UnitPrice"] = payload.unit_price
    return body


def _update_to_qbo_fields(payload: ItemUpdate) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if payload.name is not None:
        fields["Name"] = payload.name
    if payload.type is not None:
        fields["Type"] = payload.type
    if payload.income_account_id is not None:
        fields["IncomeAccountRef"] = {"value": payload.income_account_id}
    if payload.unit_price is not None:
        fields["UnitPrice"] = payload.unit_price
    return fields


# `status_code=201` on the decorator drives the OpenAPI schema; the explicit
# `JSONResponse(status_code=201, ...)` below is what actually sets the HTTP
# status at runtime. Both are needed — see FastAPI #5290.
@router.post("", status_code=201)
def create_item(
    payload: ItemCreate,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    qbo_body = _create_to_qbo_body(payload)
    try:
        resp = client.post("item", json_body=qbo_body)
    except AuthError as exc:
        return error_response(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return error_response("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return error_response("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        if _has_fault_code(exc, _DUPLICATE_NAME_CODE):
            return error_response(
                "QBO_DUPLICATE_NAME",
                f"An item named {payload.name!r} already exists",
                409,
                qbo_detail=exc.detail,
            )
        return error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    item = resp.get("Item")
    if item is None:
        return error_response(
            "QBO_ERROR",
            "QBO did not return an Item in the create response",
            502,
        )

    body = DetailResponse(data=item)
    return JSONResponse(status_code=201, content=body.model_dump(exclude_none=True))


def _load_item_for_sparse_update(
    client: QBClient, item_id: str
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Fetch the item so the caller has its current Id + SyncToken for a sparse
    write. Returns `(item, None)` on success or `(None, error_response)` so the
    PUT/DELETE flows can bail with a clean envelope on any failure.
    """
    try:
        resp = client.get(f"item/{item_id}")
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
                f"Item {item_id} not found",
                404,
                qbo_detail=exc.detail,
            )
        return None, error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    item = resp.get("Item")
    if item is None:
        return None, error_response("NOT_FOUND", f"Item {item_id} not found", 404)

    # Guard against a 200 with a partial Item body — bare subscripts on the
    # sparse body below would otherwise surface as an unhandled 500 instead of
    # the envelope.
    if item.get("Id") is None or item.get("SyncToken") is None:
        return None, error_response(
            "QBO_ERROR",
            "QBO returned an item with a missing Id or SyncToken",
            502,
        )
    return item, None


def _post_sparse_item(
    client: QBClient, item_id: str, body: dict[str, Any]
) -> JSONResponse:
    """POST a sparse item update and map the QBO faults the write flows share."""
    try:
        resp = client.post("item", json_body=body)
    except AuthError as exc:
        return error_response(exc.code, str(exc), 503)
    except TokenStoreError as exc:
        return error_response("TOKEN_STORE_FAILED", str(exc), 503)
    except RateLimitError as exc:
        return error_response("RATE_LIMITED", str(exc), 429)
    except APIError as exc:
        if _has_fault_code(exc, _STALE_SYNC_TOKEN_CODE):
            # Operation-neutral wording: this helper backs both PUT (update)
            # and DELETE (deactivate), so the message must read correctly for
            # either caller.
            return error_response(
                "QBO_STALE_SYNC_TOKEN",
                (
                    f"Item {item_id} was modified by another writer; "
                    "re-fetch and retry"
                ),
                409,
                qbo_detail=exc.detail,
            )
        if _has_fault_code(exc, _DUPLICATE_NAME_CODE):
            return error_response(
                "QBO_DUPLICATE_NAME",
                f"An item with that name already exists (item {item_id})",
                409,
                qbo_detail=exc.detail,
            )
        return error_response("QBO_ERROR", str(exc), 502, qbo_detail=exc.detail)

    updated = resp.get("Item")
    if updated is None:
        return error_response(
            "QBO_ERROR",
            "QBO did not return an Item in the sparse-update response",
            502,
        )

    body_out = DetailResponse(data=updated)
    return JSONResponse(status_code=200, content=body_out.model_dump(exclude_none=True))


@router.put("/{item_id}")
def update_item(
    item_id: str,
    payload: ItemUpdate,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    if not _ITEM_ID_RE.fullmatch(item_id):
        return error_response(
            "INVALID_PARAM",
            "item_id must be a numeric QBO entity id",
            400,
        )

    item, err = _load_item_for_sparse_update(client, item_id)
    if err is not None:
        return err
    assert item is not None  # narrow for the type checker

    sparse_body: dict[str, Any] = {
        "Id": item["Id"],
        "SyncToken": item["SyncToken"],
        "sparse": True,
        **_update_to_qbo_fields(payload),
    }
    return _post_sparse_item(client, item_id, sparse_body)


@router.delete("/{item_id}")
def deactivate_item(
    item_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    # QBO cannot hard-delete items (Amendment 1); DELETE is a sparse update
    # setting Active: false. Deactivating an already-inactive item just sends
    # the flag again and passes QBO's response through rather than masking.
    if not _ITEM_ID_RE.fullmatch(item_id):
        return error_response(
            "INVALID_PARAM",
            "item_id must be a numeric QBO entity id",
            400,
        )

    item, err = _load_item_for_sparse_update(client, item_id)
    if err is not None:
        return err
    assert item is not None  # narrow for the type checker

    sparse_body: dict[str, Any] = {
        "Id": item["Id"],
        "SyncToken": item["SyncToken"],
        "sparse": True,
        "Active": False,
    }
    return _post_sparse_item(client, item_id, sparse_body)
