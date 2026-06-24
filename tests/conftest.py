from __future__ import annotations

import httpx
import pytest

from qbsvc.config import Settings


@pytest.fixture(autouse=True)
def _offline_discovery(monkeypatch):
    """Keep tests hermetic: never let the OAuth discovery lookup hit the
    network. Defaults to the pinned fallback endpoints; the discovery
    test-suite overrides `_fetch_json` to exercise the real parse path.
    """
    from qbsvc.auth import discovery

    discovery.reset_cache()

    def _no_network(url):
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(discovery, "_fetch_json", _no_network)
    yield
    discovery.reset_cache()


@pytest.fixture(autouse=True)
def _ignore_local_env_file(monkeypatch):
    """Keep tests hermetic against a developer's local .env.

    Settings reads `.env` from the CWD (env_file in model_config), so real
    Intuit credentials on a dev machine would otherwise leak into tests that
    rely on unset env vars (e.g. missing-client-secret error paths).
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
