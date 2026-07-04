"""Startup-guard behavior once a browser launch gate exists (issue #51).

`ensure_admin_gate_configured` refuses to start on Cloud Run when the /admin/*
surface would be reachable but unprotected. Issue #51 adds two new ways for it
to be protected (a launch secret) or absent (admin routes disabled), so the
guard must accept those instead of demanding an IAM allowlist in every case.
"""
from __future__ import annotations

import pytest

from qbsvc.auth.admin_gate import ensure_admin_gate_configured
from qbsvc.config import Settings


def test_guard_accepts_launch_secret_without_allowlist(monkeypatch):
    """The public bootstrap service runs on Cloud Run with no IAM allowlist —
    its /admin/* is protected by the launch gate, so startup must succeed."""
    monkeypatch.setenv("K_SERVICE", "qb-admin")
    ensure_admin_gate_configured(
        Settings(admin_allowlist=[], admin_launch_secret="s3cret")
    )


def test_guard_accepts_admin_routes_disabled(monkeypatch):
    """The data service can disable the admin surface entirely; with no admin
    routes there is nothing to gate."""
    monkeypatch.setenv("K_SERVICE", "qb-service")
    ensure_admin_gate_configured(
        Settings(admin_allowlist=[], enable_admin_routes=False)
    )


def test_guard_still_raises_when_admin_open_and_ungated(monkeypatch):
    """Admin routes enabled, no allowlist, no launch secret → still open. Raise."""
    monkeypatch.setenv("K_SERVICE", "qb-service")
    with pytest.raises(RuntimeError, match="admin"):
        ensure_admin_gate_configured(Settings(admin_allowlist=[]))
