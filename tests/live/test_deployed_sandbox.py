"""Opt-in smoke tests against the *real* deployed sandbox service.

This is the layer the in-process suite can't reach: Cloud Run IAM, Google's
frontend (GFE) path handling, and live QBO sandbox responses. None of it runs
by default — the whole module skips unless `QBSVC_LIVE_BASE_URL` is set.

Run it:

    export QBSVC_LIVE_BASE_URL=https://qb-service-5htcalpr7a-uc.a.run.app
    # token is read from QBSVC_LIVE_ID_TOKEN, else minted via gcloud:
    uv run --extra dev --extra gcp pytest tests/live -q

The caller's identity must hold roles/run.invoker on the service. These tests
are strictly read-only — they never create, mutate, or delete QBO data, so
they're safe to run against the shared sandbox realm.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import httpx
import pytest

BASE_URL = os.environ.get("QBSVC_LIVE_BASE_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="set QBSVC_LIVE_BASE_URL to run live deployed-sandbox smoke tests",
)


def _identity_token() -> str:
    """A Google ID token for the IAM-protected service.

    Prefer an explicit `QBSVC_LIVE_ID_TOKEN` (works in CI with no gcloud);
    fall back to minting one from the local gcloud session. Skip cleanly if
    neither is available rather than failing the run.
    """
    token = os.environ.get("QBSVC_LIVE_ID_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(
            [shutil.which("gcloud") or "gcloud", "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("no QBSVC_LIVE_ID_TOKEN and gcloud is unavailable")
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip(
            "no QBSVC_LIVE_ID_TOKEN and `gcloud auth print-identity-token` "
            f"failed: {out.stderr.strip()[:200]}"
        )
    return out.stdout.strip()


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {_identity_token()}"}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30) as c:
        yield c


# --------------------------------------------------------------------------- #
# Health / readiness
# --------------------------------------------------------------------------- #


def test_readyz_ok(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}


def test_healthz_is_intercepted_by_gfe(client):
    """Regression guard for #44: Google's frontend swallows the literal
    `/healthz` path on run.app domains and answers its own 404 (an HTML page),
    never reaching our container. If this ever starts returning our JSON, GFE
    behaviour changed and the deploy/docs guidance should be revisited.
    """
    resp = client.get("/healthz")
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")


# --------------------------------------------------------------------------- #
# Read routes (live QBO sandbox)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/v1/customers", "/v1/items", "/v1/invoices"])
def test_list_routes_return_data_envelope(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("data"), list)
    assert "pagination" in body


def test_missing_customer_returns_404(client):
    """Regression guard for #45 against the live API: QBO answers a missing
    Customer with a 200 pseudonym stub; the service must still return 404.
    """
    resp = client.get("/v1/customers/99999999")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_missing_invoice_returns_404(client):
    resp = client.get("/v1/invoices/99999999")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# Admin OAuth bootstrap
# --------------------------------------------------------------------------- #


def test_oauth_start_redirects_to_intuit(client):
    resp = client.get("/admin/oauth/start", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers.get("location", "")
    assert "appcenter.intuit.com" in location
    assert "scope=com.intuit.quickbooks.accounting" in location
