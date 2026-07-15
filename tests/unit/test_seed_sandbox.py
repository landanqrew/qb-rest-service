from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from qbsvc.exceptions import APIError


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "seed_sandbox.py"

spec = importlib.util.spec_from_file_location("seed_sandbox", SCRIPT)
assert spec is not None
assert spec.loader is not None
seed_sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_sandbox)


def test_seed_customers_dry_run_records_dummy_ids_for_child_refs():
    state = {"Customer": {}, "Item": {}}
    summary = seed_sandbox.RunSummary(dry_run=True)
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

    seed_sandbox.seed_customers(
        object(), customers, state, dry_run=True, summary=summary
    )

    assert state["Customer"] == {
        "parent": "dry_run_parent",
        "child": "dry_run_child",
    }
    assert summary.to_dict()["entities"]["Customer"] == {
        "read": 2,
        "planned": 2,
        "created": 0,
        "skipped": [],
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


def test_preflight_rejects_existing_seed_state_without_force():
    state = {"Customer": {"prod-1": "sandbox-1"}, "Item": {}}

    with pytest.raises(seed_sandbox.SandboxNotFreshError, match="seed state"):
        seed_sandbox.check_target_fresh(object(), state)


def test_preflight_rejects_populated_sandbox_and_force_bypasses(monkeypatch):
    monkeypatch.setattr(
        seed_sandbox,
        "_paginated_query",
        lambda _client, entity, _where="": [{}] * (51 if entity == "Customer" else 0),
    )
    state = {"Customer": {}, "Item": {}}

    with pytest.raises(seed_sandbox.SandboxNotFreshError, match="Customer count = 51"):
        seed_sandbox.check_target_fresh(object(), state, seed_threshold=50)

    result = seed_sandbox.check_target_fresh(
        object(), state, seed_threshold=50, force=True
    )
    assert result == {
        "customer_count": 51,
        "item_count": 0,
        "threshold": 50,
        "forced": True,
        "status": "forced",
    }


def test_customer_failure_is_captured_in_run_summary(monkeypatch):
    summary = seed_sandbox.RunSummary(dry_run=False)
    state = {"Customer": {}, "Item": {}}
    error = APIError(
        400,
        "Duplicate Name Exists Error",
        raw={
            "Fault": {
                "Error": [{"code": "6240", "Message": "Duplicate Name Exists Error"}]
            }
        },
        intuit_tid="tid-123",
    )
    monkeypatch.setattr(seed_sandbox, "_post_with_retry", lambda *_args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(seed_sandbox.time, "sleep", lambda _seconds: None)

    seed_sandbox.seed_customers(
        object(),
        [{"Id": "1", "DisplayName": "Acme"}],
        state,
        dry_run=False,
        summary=summary,
    )

    customer = summary.to_dict()["entities"]["Customer"]
    assert customer["created"] == 0
    assert customer["skipped"][0]["qbo"] == {
        "status": 400,
        "detail": "Duplicate Name Exists Error",
        "intuit_tid": "tid-123",
        "errors": [{"code": "6240", "message": "Duplicate Name Exists Error"}],
    }
