from __future__ import annotations

import pytest

from qbsvc.config import Settings


@pytest.fixture(autouse=True)
def _ignore_local_env_file(monkeypatch):
    """Keep tests hermetic against a developer's local .env.

    Settings reads `.env` from the CWD (env_file in model_config), so real
    Intuit credentials on a dev machine would otherwise leak into tests that
    rely on unset env vars (e.g. missing-client-secret error paths).
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
