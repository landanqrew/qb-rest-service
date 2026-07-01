"""Bind the Cloud Run startup-probe path to a real, un-gated route.

Context (issue #44): Google's frontend intercepts the literal `/healthz` path
on `run.app` domains and 404s it before the container is reached. That's fine
for the *startup probe*, which Cloud Run runs against the container directly
(bypassing the frontend) — but it makes the probe path a quiet contract that's
easy to break: point it at a GFE-reserved-but-undefined path, an auth-gated
path, or a typo, and the symptom is a deploy that never goes healthy.

This test reads the probe path straight out of `deploy/cloud-run.yaml` and
asserts the app actually answers it 200 with no Authorization header, i.e. the
container-internal contract the probe relies on.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qbsvc.main import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLOUD_RUN_YAML = REPO_ROOT / "deploy" / "cloud-run.yaml"


def _startup_probe_path() -> str:
    """Pull the startupProbe httpGet path out of the deploy manifest.

    Text-parsed (not yaml-loaded) to keep the test free of an undeclared
    pyyaml dependency, matching how the qb-pages deploy tests read config.
    """
    text = CLOUD_RUN_YAML.read_text(encoding="utf-8")
    match = re.search(
        r"startupProbe:.*?httpGet:.*?path:\s*(\S+)", text, re.DOTALL
    )
    assert match, "no startupProbe httpGet path found in deploy/cloud-run.yaml"
    return match.group(1).strip()


def test_startup_probe_path_is_an_ungated_200_route():
    probe_path = _startup_probe_path()
    client = TestClient(create_app())

    # No Authorization header: the probe is unauthenticated, so the route must
    # not sit behind the admin gate or otherwise demand a caller identity.
    resp = client.get(probe_path)

    assert resp.status_code != 404, (
        f"startup-probe path {probe_path!r} is not a registered route"
    )
    assert resp.status_code != 403, (
        f"startup-probe path {probe_path!r} is auth-gated; the probe runs "
        "without credentials and would never pass"
    )
    assert resp.status_code == 200, (
        f"startup-probe path {probe_path!r} returned {resp.status_code}, "
        "expected 200"
    )


def test_startup_probe_path_is_documented_as_container_direct():
    """The probe deliberately uses a path the GFE may intercept externally;
    guard the comment that explains why so it isn't 'fixed' into a wrong path.
    """
    # The probe path is /healthz today; if that ever changes, this test should
    # be revisited together with the deploy.sh / iam-setup.md guidance that
    # routes *external* checks to /readyz.
    assert _startup_probe_path() == "/healthz"
