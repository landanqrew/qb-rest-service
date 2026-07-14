from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "seed_sandbox.py"

spec = importlib.util.spec_from_file_location("seed_sandbox", SCRIPT)
assert spec is not None
assert spec.loader is not None
seed_sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_sandbox)


def test_seed_customers_dry_run_records_dummy_ids_for_child_refs():
    state = {"Customer": {}, "Item": {}}
    customers = [
        {"Id": "parent", "Active": True, "DisplayName": "Parent"},
        {
            "Id": "child",
            "Active": True,
            "DisplayName": "Child",
            "ParentRef": {"value": "parent"},
            "Job": True,
        },
    ]

    seed_sandbox.seed_customers(object(), customers, state, dry_run=True)

    assert state["Customer"] == {
        "parent": "dry_run_parent",
        "child": "dry_run_child",
    }


def test_seed_items_dry_run_records_dummy_ids_and_skips_unsupported_types(monkeypatch):
    state = {"Customer": {}, "Item": {}}
    items = [
        {"Id": "service-parent", "Active": True, "Name": "Parent", "Type": "Service"},
        {
            "Id": "service-child",
            "Active": True,
            "Name": "Child",
            "Type": "Service",
            "ParentRef": {"value": "service-parent"},
            "SubItem": True,
        },
        {"Id": "group", "Active": True, "Name": "Bundle", "Type": "Group"},
        {"Id": "category", "Active": True, "Name": "Category", "Type": "Category"},
    ]
    monkeypatch.setattr(seed_sandbox, "_sandbox_account_map", lambda _sandbox: {})

    seed_sandbox.seed_items(object(), items, state, dry_run=True)

    assert state["Item"] == {
        "service-parent": "dry_run_service-parent",
        "service-child": "dry_run_service-child",
    }
