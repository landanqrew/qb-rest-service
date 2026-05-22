from __future__ import annotations


def build_query(
    entity: str,
    where: str | None = None,
    order_by: str | None = None,
    start: int = 1,
    max_results: int = 100,
) -> str:
    """Build a QBO SQL-like query string."""
    sql = f"SELECT * FROM {entity}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDERBY {order_by}"
    sql += f" STARTPOSITION {start} MAXRESULTS {max_results}"
    return sql
