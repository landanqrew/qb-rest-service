from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Pagination(BaseModel):
    """List-endpoint pagination envelope.

    `next_cursor` is omitted (None) on the last page; clients should drive
    iteration off `has_more` rather than off cursor presence so we can change
    the cursor shape later without breakage.
    """

    next_cursor: str | None = None
    has_more: bool = False


class ListResponse(BaseModel):
    data: list[dict[str, Any]] = Field(default_factory=list)
    pagination: Pagination


class DetailResponse(BaseModel):
    data: dict[str, Any]


class ErrorPayload(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    qbo_detail: str | None = None
    # Intuit's transaction id from the failing QBO response (`intuit_tid`
    # header). Surfaced so callers can quote it to Intuit support.
    intuit_tid: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorPayload
