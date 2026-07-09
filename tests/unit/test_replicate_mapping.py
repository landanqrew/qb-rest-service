"""Unit tests for the replicator's remapping/ordering core (no QBO)."""

from __future__ import annotations

import pytest

from scripts.replicate.mapping import (
    IdMap,
    RemapError,
    remap_body,
    remap_ref,
    topo_sort_by_parent,
)


def test_idmap_records_and_resolves_per_entity():
    m = IdMap()
    m.record("Account", "prod-1", "sbx-9")
    m.record("Customer", "prod-1", "sbx-5")  # same prod id, different namespace
    assert m.resolve("Account", "prod-1") == "sbx-9"
    assert m.resolve("Customer", "prod-1") == "sbx-5"


def test_resolve_unknown_id_raises_remap_error():
    m = IdMap()
    with pytest.raises(RemapError):
        m.resolve("Account", "missing")


def test_remap_ref_translates_numeric_account_ref():
    m = IdMap()
    m.record("Account", "82", "301")
    out = remap_ref("IncomeAccountRef", {"value": "82"}, m, self_entity="Item")
    assert out == {"value": "301"}


def test_remap_ref_drops_stale_name_on_ref():
    m = IdMap()
    m.record("Account", "82", "301")
    out = remap_ref(
        "IncomeAccountRef", {"value": "82", "name": "Sales"}, m, self_entity="Item"
    )
    assert out == {"value": "301"}  # name stripped so it can't shadow the Id


def test_parent_ref_resolves_within_self_entity():
    """ParentRef is a self-reference: a sub-item's parent is an Item, a
    sub-account's parent is an Account. It must resolve in the containing
    entity's namespace, not a fixed one."""
    m = IdMap()
    m.record("Item", "10", "555")
    out = remap_ref("ParentRef", {"value": "10"}, m, self_entity="Item")
    assert out == {"value": "555"}


def test_passthrough_refs_are_not_remapped():
    """CurrencyRef (ISO code) and TaxCodeRef sentinels are realm-independent
    and must survive untouched — remapping them would corrupt the value."""
    m = IdMap()
    body = {
        "Name": "Widget",
        "CurrencyRef": {"value": "USD"},
        "DefaultTaxCodeRef": {"value": "TAX"},
    }
    out = remap_body(body, m, self_entity="Item")
    assert out["CurrencyRef"] == {"value": "USD"}
    assert out["DefaultTaxCodeRef"] == {"value": "TAX"}


def test_remap_body_translates_all_account_refs_for_inventory():
    m = IdMap()
    m.record("Account", "1", "100")  # income
    m.record("Account", "2", "200")  # expense
    m.record("Account", "3", "300")  # asset
    body = {
        "Name": "Gadget",
        "Type": "Inventory",
        "IncomeAccountRef": {"value": "1"},
        "ExpenseAccountRef": {"value": "2"},
        "AssetAccountRef": {"value": "3"},
    }
    out = remap_body(body, m, self_entity="Item")
    assert out["IncomeAccountRef"] == {"value": "100"}
    assert out["ExpenseAccountRef"] == {"value": "200"}
    assert out["AssetAccountRef"] == {"value": "300"}
    assert out["Name"] == "Gadget"  # non-ref fields untouched


def test_topo_sort_emits_parents_before_children():
    rows = [
        {"Id": "3", "ParentRef": {"value": "1"}},   # child of 1
        {"Id": "1"},                                  # root
        {"Id": "2", "ParentRef": {"value": "3"}},   # grandchild (child of 3)
    ]
    ordered = [r["Id"] for r in topo_sort_by_parent(rows)]
    assert ordered.index("1") < ordered.index("3") < ordered.index("2")


def test_topo_sort_treats_out_of_slice_parent_as_root():
    """A ParentRef pointing outside the replicated set (e.g. inactive parent
    filtered out) doesn't wedge the sort; it's emitted as a root. A genuinely
    missing dependency surfaces later as a RemapError at write time."""
    rows = [{"Id": "5", "ParentRef": {"value": "999"}}]
    ordered = topo_sort_by_parent(rows)
    assert [r["Id"] for r in ordered] == ["5"]


def test_topo_sort_detects_cycle():
    rows = [
        {"Id": "a", "ParentRef": {"value": "b"}},
        {"Id": "b", "ParentRef": {"value": "a"}},
    ]
    with pytest.raises(RemapError):
        topo_sort_by_parent(rows)
