"""Integration tests for the replicator against a mock QBO transport.

A `FakeRealm` (tests/replicate_fakes.py) stands in for one QBO company: it
answers `query` reads from seeded rows and records `POST` creates, minting new
sandbox Ids so these tests prove cross-realm ID remapping happens end to end.
"""

from __future__ import annotations

import pytest

from scripts.replicate.orchestrator import replicate
from scripts.replicate.precheck import SandboxNotFreshError, check_target_fresh
from tests.replicate_fakes import FakeRealm, client_for


@pytest.fixture
def realms(tmp_path):
    def _make(source_seed):
        src = FakeRealm(source_seed)
        dst = FakeRealm()  # empty target
        return (
            src,
            dst,
            client_for(src, tmp_path, "src"),
            client_for(dst, tmp_path, "dst"),
        )

    return _make


def test_replicates_all_three_tiers(realms):
    src, dst, src_client, dst_client = realms(
        {
            "Account": [{"Id": "1", "Name": "Sales", "AccountType": "Income"}],
            "Customer": [{"Id": "1", "DisplayName": "Acme Water"}],
            "Item": [
                {
                    "Id": "1",
                    "Name": "Coliform Test",
                    "Type": "Service",
                    "IncomeAccountRef": {"value": "1", "name": "Sales"},
                }
            ],
        }
    )
    run = replicate(src_client, dst_client)
    assert run.total_created == 3
    assert run.total_skipped == 0
    assert set(dst.created) == {"Account", "Customer", "Item"}


def test_item_account_ref_is_remapped_to_new_sandbox_id(realms):
    """The whole point: an Item's IncomeAccountRef must point at the Account's
    *new* target Id, not the prod Id."""
    src, dst, src_client, dst_client = realms(
        {
            "Account": [{"Id": "77", "Name": "Sales", "AccountType": "Income"}],
            "Customer": [],
            "Item": [
                {
                    "Id": "5",
                    "Name": "Test",
                    "Type": "Service",
                    "IncomeAccountRef": {"value": "77"},
                }
            ],
        }
    )
    replicate(src_client, dst_client)
    # The Account is created first and gets the first minted id (1000).
    new_account_id = "1000"
    item_body = dst.created["Item"][0]
    assert item_body["IncomeAccountRef"]["value"] == new_account_id
    assert item_body["IncomeAccountRef"]["value"] != "77"


def test_sub_customer_parent_ref_remapped_and_ordered(realms):
    """Parent customer created before the sub-customer, and the sub-customer's
    ParentRef repointed to the parent's new sandbox Id."""
    src, dst, src_client, dst_client = realms(
        {
            "Account": [],
            "Customer": [
                {"Id": "20", "DisplayName": "Sub Job", "Job": True,
                 "ParentRef": {"value": "10"}},
                {"Id": "10", "DisplayName": "Parent Co"},
            ],
            "Item": [],
        }
    )
    replicate(src_client, dst_client)
    created = dst.created["Customer"]
    assert created[0]["DisplayName"] == "Parent Co"  # parent first
    assert created[1]["ParentRef"]["value"] == "1000"  # parent's new id


def test_item_with_missing_account_is_skipped_not_dangling(realms):
    """An Item referencing an account that wasn't replicated is recorded as
    skipped (with a reason), never written with a dangling prod Id."""
    src, dst, src_client, dst_client = realms(
        {
            "Account": [],
            "Customer": [],
            "Item": [
                {
                    "Id": "5",
                    "Name": "Orphan",
                    "Type": "Service",
                    "IncomeAccountRef": {"value": "999"},
                }
            ],
        }
    )
    run = replicate(src_client, dst_client)
    assert "Item" not in dst.created  # never written
    item_tier = next(t for t in run.tiers if t.entity == "Item")
    assert len(item_tier.skipped) == 1
    assert "unresolved reference" in item_tier.skipped[0]["reason"]


def test_qbo_rejection_captures_fault_code_and_intuit_tid(tmp_path):
    """A QBO-rejected create must land in the summary with the fault CODE and
    the intuit_tid — the two things needed to debug a headless run."""
    src = FakeRealm(
        {
            "Account": [],
            "Customer": [{"Id": "1", "DisplayName": "Dupe Co"}],
            "Item": [],
        }
    )
    dst = FakeRealm(
        fail_on={
            "Customer": {
                "code": "6240",
                "message": "Duplicate Name Exists Error",
                "intuit_tid": "abc-123-tid",
            }
        }
    )
    src_client = client_for(src, tmp_path, "src")
    dst_client = client_for(dst, tmp_path, "dst")

    run = replicate(src_client, dst_client)

    cust_tier = next(t for t in run.tiers if t.entity == "Customer")
    assert len(cust_tier.skipped) == 1
    skip = cust_tier.skipped[0]
    # The compact reason leads with the fault code and carries the tid.
    assert "6240" in skip["reason"]
    assert "abc-123-tid" in skip["reason"]
    # The structured detail is preserved for programmatic inspection.
    assert skip["qbo"]["intuit_tid"] == "abc-123-tid"
    assert skip["qbo"]["qbo_errors"][0]["code"] == "6240"


def test_skip_is_logged_structured(tmp_path, caplog):
    """The skip emits a `replicate_skipped` log record carrying the fault code
    and intuit_tid as fields (so Cloud Logging can filter on them)."""
    import logging

    src = FakeRealm({"Account": [], "Customer": [{"Id": "1", "DisplayName": "X"}], "Item": []})
    dst = FakeRealm(fail_on={"Customer": {"code": "5010", "intuit_tid": "tid-9"}})
    src_client = client_for(src, tmp_path, "src")
    dst_client = client_for(dst, tmp_path, "dst")

    with caplog.at_level(logging.WARNING, logger="qbsvc.replicate"):
        replicate(src_client, dst_client)

    skip_records = [r for r in caplog.records if r.msg == "replicate_skipped"]
    assert len(skip_records) == 1
    rec = skip_records[0]
    assert rec.skip_kind == "qbo_error"
    assert rec.intuit_tid == "tid-9"
    assert rec.qbo_errors[0]["code"] == "5010"


def test_precheck_passes_on_empty_target(realms):
    _, _, _, dst_client = realms({"Account": [], "Customer": [], "Item": []})
    result = check_target_fresh(dst_client, seed_threshold=50)
    assert result.is_fresh


def test_precheck_aborts_on_populated_target(tmp_path):
    dst = FakeRealm({"Customer": [{"Id": str(i)} for i in range(60)], "Item": []})
    dst_client = client_for(dst, tmp_path, "dst")
    with pytest.raises(SandboxNotFreshError) as exc:
        check_target_fresh(dst_client, seed_threshold=50)
    assert "Clear Data and Reset" in str(exc.value)


def test_precheck_force_bypasses_abort(tmp_path):
    dst = FakeRealm({"Customer": [{"Id": str(i)} for i in range(60)], "Item": []})
    dst_client = client_for(dst, tmp_path, "dst")
    result = check_target_fresh(dst_client, seed_threshold=50, force=True)
    assert not result.is_fresh  # reported honestly, but no raise
