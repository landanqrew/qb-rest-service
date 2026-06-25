from __future__ import annotations

import httpx
import pytest

from qbsvc.auth import discovery
from qbsvc.config import Settings

# A realistic Intuit discovery payload (trimmed to the fields we consume).
_DISCOVERY_PAYLOAD = {
    "issuer": "https://oauth.platform.intuit.com/op/v1",
    "authorization_endpoint": "https://appcenter.intuit.com/connect/oauth2",
    "token_endpoint": "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
    "revocation_endpoint": "https://developer.api.intuit.com/v2/oauth2/tokens/revoke",
}


@pytest.fixture
def settings():
    return Settings(intuit_environment="production")


def test_get_discovery_parses_endpoints(settings, monkeypatch):
    monkeypatch.setattr(discovery, "_fetch_json", lambda url: dict(_DISCOVERY_PAYLOAD))

    doc = discovery.get_discovery(settings)

    assert doc.authorization_endpoint == _DISCOVERY_PAYLOAD["authorization_endpoint"]
    assert doc.token_endpoint == _DISCOVERY_PAYLOAD["token_endpoint"]
    assert doc.revocation_endpoint == _DISCOVERY_PAYLOAD["revocation_endpoint"]


def test_production_and_sandbox_use_distinct_documents(monkeypatch):
    requested: list[str] = []

    def _record(url):
        requested.append(url)
        return dict(_DISCOVERY_PAYLOAD)

    monkeypatch.setattr(discovery, "_fetch_json", _record)

    discovery.get_discovery(Settings(intuit_environment="production"))
    discovery.get_discovery(Settings(intuit_environment="sandbox"))

    assert requested == [
        discovery.PRODUCTION_DISCOVERY_URL,
        discovery.SANDBOX_DISCOVERY_URL,
    ]


def test_get_discovery_caches_within_ttl(settings, monkeypatch):
    calls = {"n": 0}

    def _counting(url):
        calls["n"] += 1
        return dict(_DISCOVERY_PAYLOAD)

    monkeypatch.setattr(discovery, "_fetch_json", _counting)
    clock = iter([0.0, 1.0, 2.0])

    discovery.get_discovery(settings, clock=lambda: next(clock))
    discovery.get_discovery(settings, clock=lambda: next(clock))

    assert calls["n"] == 1, "second call within TTL should hit the cache"


def test_get_discovery_refetches_after_ttl(settings, monkeypatch):
    calls = {"n": 0}

    def _counting(url):
        calls["n"] += 1
        return dict(_DISCOVERY_PAYLOAD)

    monkeypatch.setattr(discovery, "_fetch_json", _counting)
    # Second read is well past the 24h TTL.
    clock = iter([0.0, discovery._DISCOVERY_TTL_SECONDS + 1.0])

    discovery.get_discovery(settings, clock=lambda: next(clock))
    discovery.get_discovery(settings, clock=lambda: next(clock))

    assert calls["n"] == 2


def test_falls_back_on_network_error(settings, monkeypatch):
    def _boom(url):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(discovery, "_fetch_json", _boom)

    doc = discovery.get_discovery(settings)

    assert doc == discovery.FALLBACK


def test_falls_back_on_malformed_document(settings, monkeypatch):
    # Missing `token_endpoint` — a parse failure must degrade to fallback,
    # not raise into the OAuth flow.
    monkeypatch.setattr(
        discovery,
        "_fetch_json",
        lambda url: {"authorization_endpoint": "https://x"},
    )

    doc = discovery.get_discovery(settings)

    assert doc == discovery.FALLBACK


def test_fallback_is_not_cached(settings, monkeypatch):
    """A transient failure must not pin the fallback for the full TTL — the
    next call should retry discovery and pick up a now-healthy endpoint."""
    state = {"fail": True}

    def _flaky(url):
        if state["fail"]:
            raise httpx.ConnectError("unreachable")
        return dict(_DISCOVERY_PAYLOAD)

    monkeypatch.setattr(discovery, "_fetch_json", _flaky)

    first = discovery.get_discovery(settings)
    assert first == discovery.FALLBACK

    state["fail"] = False
    second = discovery.get_discovery(settings)
    assert second.token_endpoint == _DISCOVERY_PAYLOAD["token_endpoint"]
