"""Write side: per-entity payload builders + the single create() choke-point.

Every create flows through `Writer.create`, so a QBO `/batch` backend (30 ops
per call — the performance lever the Invoice tier will want) can be slotted in
here later without touching the orchestrator. For v1 it's one sequential POST
per record, respecting the client's own TokenBucket.

The payload builders strip a prod entity down to the fields worth replicating
and hand off ref-remapping to `mapping.remap_body`. The item builder mirrors
the shape the service's own `POST /v1/items` handler emits
(qbsvc.routes.items._create_to_qbo_body) so replicated items round-trip
identically to app-created ones.
"""

from __future__ import annotations

from qbsvc.api.client import QBClient

from .mapping import IdMap, remap_body

# QBO's create-response wraps the new entity under its PascalCase name, e.g.
# {"Item": {"Id": "42", ...}}. Same key we POST to (capitalized).
def _created_id(resp: dict, entity: str) -> str:
    obj = resp.get(entity) or {}
    new_id = obj.get("Id")
    if not new_id:
        raise RuntimeError(
            f"QBO create response for {entity} had no Id: {resp!r}"
        )
    return str(new_id)


class Writer:
    """Creates entities in the target realm and records the prod→sandbox map."""

    def __init__(self, client: QBClient, id_map: IdMap):
        self._client = client
        self._id_map = id_map

    def create(self, entity: str, body: dict) -> str:
        """POST one entity, record its id-mapping, return the new sandbox Id.

        Single choke-point for all writes — the seam where batching would go.
        """
        resp = self._client.post(entity.lower(), json_body=body)
        return _created_id(resp, entity)

    def replicate(self, entity: str, source_row: dict) -> str:
        """Build → remap → create → record for one source row.

        Returns the new sandbox Id. Raises `mapping.RemapError` if a ref points
        at a prod Id that wasn't replicated first (a topological-order bug), so
        the orchestrator can record the record as skipped rather than writing a
        dangling reference.
        """
        prod_id = str(source_row["Id"])
        body = build_body(entity, source_row)
        body = remap_body(body, self._id_map, self_entity=entity)
        sandbox_id = self.create(entity, body)
        self._id_map.record(entity, prod_id, sandbox_id)
        return sandbox_id


# ---------------------------------------------------------------------------
# Per-entity payload builders (prod row -> create body, refs still prod-Id'd)
# ---------------------------------------------------------------------------

def build_body(entity: str, row: dict) -> dict:
    builder = _BUILDERS.get(entity)
    if builder is None:
        raise ValueError(f"No payload builder for entity {entity!r}")
    return builder(row)


def _account_body(row: dict) -> dict:
    """QBO Account create. Name + AccountType are the required identity;
    AcctNum, Classification, and ParentRef/SubAccount preserve the CoA shape.
    """
    body: dict = {"Name": row["Name"]}
    _copy_if(body, row, "AccountType")
    _copy_if(body, row, "AccountSubType")
    _copy_if(body, row, "AcctNum")
    _copy_if(body, row, "Classification")
    _copy_if(body, row, "Description")
    if row.get("SubAccount") and isinstance(row.get("ParentRef"), dict):
        body["SubAccount"] = True
        body["ParentRef"] = dict(row["ParentRef"])  # remapped downstream
    return body


def _customer_body(row: dict) -> dict:
    """QBO Customer create. DisplayName is the only hard requirement; the rest
    reproduce the real naming/contact detail that makes the sandbox realistic.
    Sub-customers (Jobs) carry Job=True + ParentRef.
    """
    body: dict = {"DisplayName": row["DisplayName"]}
    for field_name in (
        "CompanyName",
        "GivenName",
        "FamilyName",
        "MiddleName",
        "Suffix",
        "Title",
        "PrimaryEmailAddr",
        "PrimaryPhone",
        "BillAddr",
        "ShipAddr",
        "Notes",
    ):
        _copy_if(body, row, field_name)
    if row.get("Job") and isinstance(row.get("ParentRef"), dict):
        body["Job"] = True
        body["ParentRef"] = dict(row["ParentRef"])  # remapped downstream
    return body


def _item_body(row: dict) -> dict:
    """QBO Item create, mirroring the service's own item create body shape.

    Account refs are copied through (remapped downstream). Inventory items
    additionally need Expense/Asset refs + opening qty/date; we replicate
    whatever the source populated rather than re-deriving it.
    """
    body: dict = {"Name": row["Name"], "Type": row["Type"]}
    _copy_if(body, row, "IncomeAccountRef")
    _copy_if(body, row, "UnitPrice")
    _copy_if(body, row, "Description")
    if row.get("Type") == "Inventory":
        body["TrackQtyOnHand"] = True
        _copy_if(body, row, "ExpenseAccountRef")
        _copy_if(body, row, "AssetAccountRef")
        _copy_if(body, row, "QtyOnHand")
        _copy_if(body, row, "InvStartDate")
    if row.get("SubItem") and isinstance(row.get("ParentRef"), dict):
        body["SubItem"] = True
        body["ParentRef"] = dict(row["ParentRef"])  # remapped downstream
    return body


def _copy_if(body: dict, row: dict, key: str) -> None:
    """Copy `key` from source row into body when present and non-null."""
    if row.get(key) is not None:
        body[key] = row[key]


_BUILDERS = {
    "Account": _account_body,
    "Customer": _customer_body,
    "Item": _item_body,
}
