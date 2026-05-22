from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from qbsvc.api.client import QBClient
from qbsvc.deps import get_qb_client
from qbsvc.routes._common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    get_entity_detail,
    list_entity,
)

router = APIRouter(prefix="/customers", tags=["customers"])


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
    # QBO's default SELECT * filters to Active=true; the explicit IN clause is
    # the documented way to also include inactive rows when active=false.
    active_clause = "Active = true" if active else "Active IN (true, false)"
    return list_entity(
        client,
        entity="Customer",
        extra_filters=[active_clause],
        modified_since=modified_since,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{customer_id}")
def get_customer(
    customer_id: str,
    client: Annotated[QBClient, Depends(get_qb_client)],
) -> JSONResponse:
    return get_entity_detail(client, entity="Customer", entity_id=customer_id)
