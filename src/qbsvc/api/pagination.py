from __future__ import annotations

from collections.abc import Generator

from qbsvc.api.client import QBClient
from qbsvc.exceptions import PaginationError

DEFAULT_PAGE_SIZE = 100  # QBO max is 1000, 100 is a reasonable default


def paginate(
    client: QBClient,
    entity: str,
    where: str | None = None,
    order_by: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    limit: int | None = None,
) -> Generator[dict, None, None]:
    """Auto-paginate a QBO query using startPosition and maxResults.

    Args:
        client: QBClient instance.
        entity: QBO entity name (e.g. 'Customer', 'Invoice').
        where: Optional WHERE clause (without 'WHERE' keyword).
        order_by: Optional ORDER BY clause (without 'ORDER BY' keyword).
        page_size: Number of items per page.
        limit: Max total items to yield. None = unlimited.
    """
    start = 1  # QBO uses 1-based indexing
    yielded = 0
    effective_size = min(page_size, limit) if limit else page_size

    while True:
        sql = f"SELECT * FROM {entity}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDERBY {order_by}"
        sql += f" STARTPOSITION {start} MAXRESULTS {effective_size}"

        results = client.query(sql)

        if not results:
            return

        for item in results:
            yield item
            yielded += 1
            if limit and yielded >= limit:
                return

        if len(results) < effective_size:
            return

        start += len(results)

        if limit:
            remaining = limit - yielded
            effective_size = min(page_size, remaining)
