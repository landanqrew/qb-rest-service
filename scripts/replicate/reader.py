"""Exhaustive paged reads from a source realm.

Wraps `build_query` + `QBClient.query` to pull *every* row of an entity by
following QBO's STARTPOSITION offset until a short page signals the end. Kept
separate from the writer so the read side is trivially testable against a mock
transport and reusable for the future Invoice tier.
"""

from __future__ import annotations

from qbsvc.api.client import QBClient
from qbsvc.api.queries import build_query

# QBO's hard MAXRESULTS ceiling is 1000. We page at exactly that; the loop
# terminates when a page comes back shorter than the page size (including 0),
# so unlike the route layer's `limit+1` probe there's no lost boundary row.
PAGE_SIZE = 1000


def read_all(
    client: QBClient,
    entity: str,
    *,
    where: str | None = None,
    page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Return every row of `entity` from the realm `client` is bound to.

    `where` is interpolated verbatim into the SQL (QBO has no bound params);
    callers pass only literal clauses they construct, never external input.
    """
    rows: list[dict] = []
    start = 1  # QBO STARTPOSITION is 1-based.
    while True:
        sql = build_query(
            entity,
            where=where,
            start=start,
            max_results=page_size,
        )
        page = client.query(sql)
        rows.extend(page)
        # A full page means there may be more; a short (or empty) page is the
        # last one. QBO returns at most `page_size` rows per call.
        if len(page) < page_size:
            break
        start += len(page)
    return rows


def read_active(client: QBClient, entity: str, **kwargs) -> list[dict]:
    """Convenience: only `Active = true` rows.

    Replication copies the live catalogue; deactivated ("(deleted)") prod rows
    are noise in a fresh sandbox. Callers needing inactive rows use `read_all`.
    """
    return read_all(client, entity, where="Active = true", **kwargs)
