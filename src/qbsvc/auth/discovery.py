from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from qbsvc.config import Settings

_log = logging.getLogger("qbsvc.discovery")

# Intuit's OpenID Connect discovery documents. Production and sandbox expose
# the same authorize/token endpoints today, but Intuit publishes them
# separately and reserves the right to diverge — pick the one matching the
# configured environment.
PRODUCTION_DISCOVERY_URL = (
    "https://developer.api.intuit.com/.well-known/openid_configuration"
)
SANDBOX_DISCOVERY_URL = (
    "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration"
)

# Endpoints change rarely; cache the parsed document per environment and
# re-check once a day so a long-lived instance still picks up an Intuit-side
# change without a restart.
_DISCOVERY_TTL_SECONDS = 86_400


@dataclass(frozen=True)
class DiscoveryDocument:
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str


# Used when the discovery document can't be fetched or parsed. Mirrors the
# values Intuit currently publishes so the OAuth flow survives a transient
# discovery outage.
FALLBACK = DiscoveryDocument(
    authorization_endpoint="https://appcenter.intuit.com/connect/oauth2",
    token_endpoint="https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
    revocation_endpoint="https://developer.api.intuit.com/v2/oauth2/tokens/revoke",
)

_lock = threading.Lock()
_cache: dict[str, tuple[float, DiscoveryDocument]] = {}


def get_discovery(settings: Settings, *, clock=time.monotonic) -> DiscoveryDocument:
    """Return Intuit's OAuth endpoints, sourced from the discovery document.

    Cached per environment for `_DISCOVERY_TTL_SECONDS`. On any fetch or parse
    failure, returns the pinned `FALLBACK` so the OAuth flow never breaks on a
    transient discovery outage.
    """
    env = settings.intuit_environment
    now = clock()
    with _lock:
        cached = _cache.get(env)
        if cached is not None and now - cached[0] < _DISCOVERY_TTL_SECONDS:
            return cached[1]

    doc = _fetch(env)
    if doc is None:
        # Don't cache the fallback — retry discovery on the next call rather
        # than pinning the fallback for the full TTL after a transient blip.
        return FALLBACK

    with _lock:
        _cache[env] = (now, doc)
    return doc


def reset_cache() -> None:
    """Clear the per-environment discovery cache. Used by tests."""
    with _lock:
        _cache.clear()


def _fetch(env: str) -> DiscoveryDocument | None:
    url = SANDBOX_DISCOVERY_URL if env == "sandbox" else PRODUCTION_DISCOVERY_URL
    try:
        data = _fetch_json(url)
        return DiscoveryDocument(
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            revocation_endpoint=data["revocation_endpoint"],
        )
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        _log.warning(
            "discovery_fetch_failed; falling back to pinned endpoints",
            extra={"discovery_url": url, "error": str(exc)},
        )
        return None


def _fetch_json(url: str) -> dict:
    """Single network seam for discovery — patched in tests to stay offline."""
    resp = httpx.get(url, headers={"Accept": "application/json"}, timeout=10)
    resp.raise_for_status()
    return resp.json()
