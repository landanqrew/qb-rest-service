#!/usr/bin/env python3
"""Update and verify two custom fields on production invoice 105977.

Set ``poNumber`` and ``salesRep`` below, then run:

    QBSVC_LIVE_BASE_URL=https://YOUR-QB-SERVICE.run.app \
      uv run python scripts/update_production_invoice_custom_fields.py

The script uses QBSVC_LIVE_ID_TOKEN when set. Otherwise, it asks the local
gcloud CLI for an identity token. It always reads and validates the invoice
before displaying the sparse custom-field payload and asking for confirmation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, NoReturn

import httpx


# Change only these two values for each test (QBO limit: 31 characters each).
# ORIGINALS:
# PO Number: Black Swan
# Sales Rep: Jack Horton
poNumber = "Black Swan API"
salesRep = "Jack Horton API"


INVOICE_ID = "105977"
EXPECTED_DOC_NUMBER = "26-08-043"
EXPECTED_CUSTOMER_ID = "5321"
PO_NUMBER_FIELD_NAME = "P.O. Number"
SALES_REP_FIELD_NAME = "Sales Rep"

# These are compared before and after the sparse update. SyncToken, MetaData,
# and CustomField are intentionally excluded because the write changes them.
UNCHANGED_FIELDS = (
    "Line",
    "TotalAmt",
    "Balance",
    "TxnDate",
    "DueDate",
    "DocNumber",
    "CustomerRef",
    "BillAddr",
    "ShipAddr",
    "ShipFromAddr",
)


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def identity_token() -> str:
    explicit_token = os.environ.get("QBSVC_LIVE_ID_TOKEN", "").strip()
    if explicit_token:
        return explicit_token

    gcloud = shutil.which("gcloud")
    if not gcloud:
        fail("set QBSVC_LIVE_ID_TOKEN or install and authenticate gcloud")

    try:
        result = subprocess.run(
            [gcloud, "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        fail("gcloud timed out while minting an identity token")

    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip()[:300]
        fail(f"gcloud could not mint an identity token: {detail}")
    return result.stdout.strip()


def response_data(response: httpx.Response, operation: str) -> dict[str, Any]:
    if not response.is_success:
        fail(f"{operation} failed with HTTP {response.status_code}: {response.text}")
    try:
        body = response.json()
    except ValueError:
        fail(f"{operation} returned non-JSON: {response.text[:300]}")
    data = body.get("data")
    if not isinstance(data, dict):
        fail(f"{operation} returned an unexpected response: {body}")
    return data


def validate_target(invoice: dict[str, Any]) -> None:
    customer_id = str((invoice.get("CustomerRef") or {}).get("value", ""))
    checks = {
        "Id": (str(invoice.get("Id", "")), INVOICE_ID),
        "DocNumber": (str(invoice.get("DocNumber", "")), EXPECTED_DOC_NUMBER),
        "CustomerRef.value": (customer_id, EXPECTED_CUSTOMER_ID),
    }
    mismatches = [
        f"{name}: got {actual!r}, expected {expected!r}"
        for name, (actual, expected) in checks.items()
        if actual != expected
    ]
    if mismatches:
        fail("refusing to update the wrong invoice:\n  " + "\n  ".join(mismatches))


def custom_field_by_name(invoice: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [
        field
        for field in invoice.get("CustomField", [])
        if field.get("Name") == name
    ]
    if len(matches) != 1:
        fail(f"expected exactly one {name!r} custom field, found {len(matches)}")
    field = matches[0]
    definition_id = str(field.get("DefinitionId", ""))
    if not definition_id.isascii() or not definition_id.isdigit():
        fail(f"custom field {name!r} has invalid DefinitionId {definition_id!r}")
    return field


def custom_fields_payload(invoice: dict[str, Any]) -> dict[str, Any]:
    po_definition_id = str(
        custom_field_by_name(invoice, PO_NUMBER_FIELD_NAME)["DefinitionId"]
    )
    rep_definition_id = str(
        custom_field_by_name(invoice, SALES_REP_FIELD_NAME)["DefinitionId"]
    )
    return {
        "custom_fields": [
            {"definition_id": po_definition_id, "value": poNumber},
            {"definition_id": rep_definition_id, "value": salesRep},
        ],
    }


def custom_field_values(invoice: dict[str, Any]) -> dict[str, Any]:
    return {
        str(field.get("DefinitionId")): field.get("StringValue")
        for field in invoice.get("CustomField", [])
        if field.get("DefinitionId") is not None
    }


def main() -> None:
    if len(poNumber) > 31 or len(salesRep) > 31:
        fail("poNumber and salesRep must each be at most 31 characters")

    base_url = os.environ.get("QBSVC_LIVE_BASE_URL", "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        fail("set QBSVC_LIVE_BASE_URL to the production qb-service HTTPS URL")

    invoice_url = f"{base_url}/v1/invoices/{INVOICE_ID}"
    headers = {"Authorization": f"Bearer {identity_token()}"}
    with httpx.Client(headers=headers, timeout=60) as client:
        current = response_data(client.get(invoice_url), "initial invoice read")
        validate_target(current)
        payload = custom_fields_payload(current)

        print(f"Target: invoice {INVOICE_ID} / {EXPECTED_DOC_NUMBER}")
        print(f"Current custom fields: {json.dumps(custom_field_values(current))}")
        print("Sparse custom-field request (no other invoice fields are sent):")
        print(json.dumps(payload, indent=2))
        confirmation = input(f'Type "UPDATE {INVOICE_ID}" to write to production: ')
        if confirmation != f"UPDATE {INVOICE_ID}":
            fail("confirmation did not match; no update was sent")

        custom_fields_url = f"{invoice_url}/custom-fields"
        updated = response_data(
            client.patch(custom_fields_url, json=payload), "custom-field update"
        )
        validate_target(updated)

        # Read once more instead of trusting only the write response, proving
        # that the values persisted through qb-service into QBO.
        persisted = response_data(client.get(invoice_url), "verification read")
        validate_target(persisted)
        actual = custom_field_values(persisted)
        po_definition_id = payload["custom_fields"][0]["definition_id"]
        rep_definition_id = payload["custom_fields"][1]["definition_id"]
        expected = {po_definition_id: poNumber, rep_definition_id: salesRep}
        failures = {
            definition_id: {"expected": value, "actual": actual.get(definition_id)}
            for definition_id, value in expected.items()
            if actual.get(definition_id) != value
        }
        if failures:
            fail(f"update returned successfully but verification failed: {failures}")

        unexpected_changes = {
            field: {"before": current.get(field), "after": persisted.get(field)}
            for field in UNCHANGED_FIELDS
            if current.get(field) != persisted.get(field)
        }
        if unexpected_changes:
            fail(f"non-custom invoice fields changed unexpectedly: {unexpected_changes}")

        print("Verified in QBO:")
        print(f"  P.O. Number (DefinitionId {po_definition_id}): {actual[po_definition_id]!r}")
        print(f"  Sales Rep   (DefinitionId {rep_definition_id}): {actual[rep_definition_id]!r}")
        print("Verified that all monitored non-custom invoice fields are unchanged.")


if __name__ == "__main__":
    main()
