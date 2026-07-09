"""ID remapping and dependency ordering — the correctness core of the replicator.

When an entity is copied prod → sandbox, QBO mints a *new* Id in the target.
Every reference to that entity (an Item's `IncomeAccountRef`, a sub-customer's
`ParentRef`, …) is stored by Id, so each ref must be rewritten from the prod Id
to the freshly-created sandbox Id at write time. This module owns:

  * `IdMap` — the per-entity `{prod_id -> sandbox_id}` accumulator.
  * `remap_ref` / `remap_body` — apply the per-ref-type policy (remap numeric
    Ids, pass through realm-independent sentinels like CurrencyRef's "USD").
  * `topo_sort_by_parent` — order a single entity type so parents precede
    children (QBO rejects a ParentRef pointing at an Id that doesn't exist yet).

Everything here is pure (no QBO calls), so it's unit-testable without a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class RemapError(RuntimeError):
    """A ref pointed at a prod Id that was never replicated into the target.

    Signals a real ordering/dependency bug (e.g. an Item referencing an Account
    that wasn't copied) rather than being silently dropped — the caller decides
    whether to abort the run or record it as a skipped record.
    """


@dataclass
class IdMap:
    """Accumulates `{prod_id -> sandbox_id}` maps, one namespace per entity type.

    Entity keys are the QBO PascalCase names ("Account", "Customer", "Item").
    Populated incrementally as each create succeeds; read back when remapping a
    dependent entity's refs.
    """

    _maps: dict[str, dict[str, str]] = field(default_factory=dict)

    def record(self, entity: str, prod_id: str, sandbox_id: str) -> None:
        self._maps.setdefault(entity, {})[prod_id] = sandbox_id

    def resolve(self, entity: str, prod_id: str) -> str:
        try:
            return self._maps[entity][prod_id]
        except KeyError as exc:
            raise RemapError(
                f"No sandbox Id recorded for {entity} prod Id {prod_id!r}. "
                "The referenced entity was not replicated (or not yet) — check "
                "the topological order and that its tier ran first."
            ) from exc

    def get(self, entity: str) -> dict[str, str]:
        """The full prod→sandbox map for one entity (for the run summary)."""
        return dict(self._maps.get(entity, {}))


# ---------------------------------------------------------------------------
# Per-ref-type policy
# ---------------------------------------------------------------------------

# Refs whose `.value` is a numeric QBO entity Id that changes across realms and
# MUST be remapped. Each maps to the entity type whose IdMap namespace holds the
# translation. This is the allow-list: a ref not named here is passed through
# unchanged (see `remap_body`), which is the safe default for value-refs like
# CurrencyRef ("USD") and TaxCodeRef sentinels ("TAX"/"NON") that are identical
# in every realm and would be corrupted by remapping.
REF_TARGET_ENTITY: dict[str, str] = {
    "ParentRef": None,  # resolved to the *containing* entity type at call time
    "IncomeAccountRef": "Account",
    "ExpenseAccountRef": "Account",
    "AssetAccountRef": "Account",
    # Future (Invoice): "CustomerRef": "Customer", "ItemRef": "Item".
}


def remap_ref(
    ref_name: str,
    ref_value: dict,
    id_map: IdMap,
    *,
    self_entity: str,
) -> dict:
    """Return a rewritten copy of a single `{"value": ...}` ref dict.

    `self_entity` names the entity being written, so `ParentRef` (a self-
    reference — a sub-account's parent is an Account, a sub-item's is an Item)
    resolves within the right namespace. Refs outside REF_TARGET_ENTITY are
    returned unchanged.
    """
    if ref_name not in REF_TARGET_ENTITY:
        return ref_value
    if not isinstance(ref_value, dict) or "value" not in ref_value:
        return ref_value

    target_entity = REF_TARGET_ENTITY[ref_name] or self_entity
    new_ref = dict(ref_value)
    new_ref["value"] = id_map.resolve(target_entity, str(ref_value["value"]))
    # QBO ignores/echoes `name` on refs; drop it so a stale prod name can't
    # shadow the resolved Id or trip a name-mismatch validation in the target.
    new_ref.pop("name", None)
    return new_ref


def remap_body(body: dict, id_map: IdMap, *, self_entity: str) -> dict:
    """Return a copy of a create body with all known Id refs remapped.

    Shallow by design: QBO's Account/Customer/Item refs all live at the top
    level. Nested-line remapping (for future Invoice support) is a separate,
    explicit pass, not something we want to do implicitly here.
    """
    out = dict(body)
    for key, value in body.items():
        if key in REF_TARGET_ENTITY:
            out[key] = remap_ref(key, value, id_map, self_entity=self_entity)
    return out


# ---------------------------------------------------------------------------
# Intra-type dependency ordering
# ---------------------------------------------------------------------------

def topo_sort_by_parent(
    records: list[dict],
    *,
    parent_ref: str = "ParentRef",
    id_field: str = "Id",
) -> list[dict]:
    """Order records so every parent precedes its children.

    QBO hierarchies (sub-accounts, sub-customers/Jobs, sub-items) express the
    parent as `<parent_ref>.value = <parent Id>`. A child cannot be created
    before its parent exists in the target, so we emit roots first, then peel
    off records whose parent has already been emitted.

    Records whose parent Id is not present in this set (a parent outside the
    replicated slice, e.g. filtered out as inactive) are treated as roots —
    the caller's remap will surface a RemapError if that parent truly wasn't
    created, which is the honest failure rather than a silent drop.

    QBO forbids cycles in these hierarchies, so this always terminates; if a
    cycle ever appeared we'd rather raise than loop forever.
    """
    by_id = {str(r.get(id_field)): r for r in records if r.get(id_field) is not None}

    def parent_id(rec: dict) -> str | None:
        ref = rec.get(parent_ref)
        if isinstance(ref, dict) and ref.get("value") is not None:
            return str(ref["value"])
        return None

    ordered: list[dict] = []
    emitted: set[str] = set()
    # Records with a parent that isn't in this slice are eligible immediately.
    pending = list(records)

    while pending:
        progressed = False
        still_pending: list[dict] = []
        for rec in pending:
            pid = parent_id(rec)
            if pid is None or pid not in by_id or pid in emitted:
                ordered.append(rec)
                rid = rec.get(id_field)
                if rid is not None:
                    emitted.add(str(rid))
                progressed = True
            else:
                still_pending.append(rec)
        pending = still_pending
        if not progressed:
            raise RemapError(
                "Cycle or unresolved parent chain detected while ordering "
                f"{parent_ref} hierarchy — {len(pending)} record(s) never "
                "became eligible. QBO should forbid this; inspect the source data."
            )
    return ordered
