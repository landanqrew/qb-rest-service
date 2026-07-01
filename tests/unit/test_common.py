"""Unit tests for the shared route helpers in qbsvc.routes._common.

These back the 404 contract that every detail endpoint relies on: QBO signals
a missing entity two different ways depending on the entity type, and these
helpers normalize both. The route-level integration tests exercise the wiring;
these pin the detection logic directly.
"""
from __future__ import annotations

import pytest

from qbsvc.exceptions import APIError
from qbsvc.routes._common import is_not_found, is_pseudonym_miss, parse_iso_date


# --------------------------------------------------------------------------- #
# is_not_found — fault-style misses (Item, Invoice, …)
# --------------------------------------------------------------------------- #


def test_is_not_found_true_on_plain_404():
    assert is_not_found(APIError(404, "whatever")) is True


def test_is_not_found_true_on_400_object_not_found():
    # QBO's actual miss shape for Item/Invoice: HTTP 400, "Object Not Found".
    assert is_not_found(APIError(400, "Object Not Found : it's gone")) is True


def test_is_not_found_is_case_insensitive():
    assert is_not_found(APIError(400, "OBJECT NOT FOUND")) is True


def test_is_not_found_false_on_other_400():
    # A validation 400 that isn't a miss must not be masked as a 404.
    assert is_not_found(APIError(400, "Required param missing")) is False


def test_is_not_found_false_on_500():
    assert is_not_found(APIError(500, "Object Not Found")) is False


# --------------------------------------------------------------------------- #
# is_pseudonym_miss — 200 stub misses (Customer, Vendor, …)
# --------------------------------------------------------------------------- #


def test_pseudonym_stub_is_a_miss():
    # The real QBO sandbox response for a non-existent Customer id: a sparse
    # stub with V4IDPseudonym and no Id/SyncToken.
    stub = {
        "BillAddr": {"Id": "1"},
        "Active": False,
        "domain": "QBO",
        "sparse": False,
        "V4IDPseudonym": "99999999",
    }
    assert is_pseudonym_miss(stub) is True


def test_real_entity_is_not_a_miss():
    real = {"Id": "42", "SyncToken": "0", "DisplayName": "Customer 42"}
    assert is_pseudonym_miss(real) is False


def test_entity_with_id_but_no_pseudonym_is_not_a_miss():
    # An entity that has an Id is present, full stop — even if some optional
    # fields are absent.
    assert is_pseudonym_miss({"Id": "7"}) is False


# --------------------------------------------------------------------------- #
# parse_iso_date — guards SQL-literal date inputs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["2026-01-01", "1999-12-31"])
def test_parse_iso_date_accepts_strict_calendar_dates(value):
    assert parse_iso_date(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "2026-1-1",       # not zero-padded
        "2026/01/01",     # wrong separator
        "2026-13-01",     # impossible month
        "20260101",       # basic format (date.fromisoformat would accept — we reject)
        "2026-01-01'; DROP",  # injection attempt
        "",
    ],
)
def test_parse_iso_date_rejects_non_strict_or_malicious(value):
    assert parse_iso_date(value) is None
