"""Deploy-contract invariants for the qb-admin browser bootstrap service (#51).

Static assertions over the deploy artifacts so the security-relevant shape of
the public OAuth bootstrap service can't silently drift: it must be public,
serve admin OAuth only (data routes off), be gated by the launch secret rather
than an IAM allowlist, and share the token secret with qb-service.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY = REPO_ROOT / "deploy"

SCRIPT = DEPLOY / "qb-admin.deploy.sh"
YAML = DEPLOY / "qb-admin.cloud-run.yaml"
DOC = DEPLOY / "qb-admin-setup.md"


def test_deploy_artifacts_exist():
    assert SCRIPT.is_file()
    assert YAML.is_file()
    assert DOC.is_file()


def test_deploy_script_is_public_and_data_routes_off():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--allow-unauthenticated" in text
    assert "QBSVC_ENABLE_DATA_ROUTES=false" in text
    # The launch secret must be wired from Secret Manager.
    assert "QBSVC_ADMIN_LAUNCH_SECRET=${SECRET_ADMIN_LAUNCH}:latest" in text
    # Shares the token secret with qb-service.
    assert "QBSVC_SECRET_NAME_TOKENS=${SECRET_TOKENS}" in text
    # A public service must NOT assign an IAM email allowlist (meaningless
    # here). Comments may mention it by name; only the assignment form is banned.
    assert "QBSVC_ADMIN_ALLOWLIST=" not in text


def test_deploy_script_uses_configurable_shared_image_name():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'IMAGE_NAME="${IMAGE_NAME:-qb-service}"' in text
    assert '${AR_REPO}/${IMAGE_NAME}:${GIT_SHA}' in text
    assert "${AR_REPO}/qb-service:${GIT_SHA}" not in text


def test_deploy_yaml_is_public_shape_and_gated():
    text = YAML.read_text(encoding="utf-8")
    assert "qb-admin" in text.lower()
    # Scope the value to the SAME env entry as the name, so an unrelated
    # `value: "false"` elsewhere can't satisfy the check.
    assert (
        'name: QBSVC_ENABLE_DATA_ROUTES\n              value: "false"' in text
    )
    assert "SECRET_ADMIN_LAUNCH" in text
    assert "SECRET_TOKENS" in text
    # No IAM allowlist env declared on the public service (comments may name it).
    assert "name: QBSVC_ADMIN_ALLOWLIST" not in text
    # Dedicated runtime SA, not qb-service's.
    assert "qb-admin-runtime@" in text


def test_deploy_yaml_and_script_agree_on_intuit_env_var():
    """The manifest must carry the production/sandbox toggle so applying it
    directly stays in parity with qb-admin.deploy.sh."""
    assert "QBSVC_INTUIT_ENVIRONMENT" in YAML.read_text(encoding="utf-8")
    assert "QBSVC_INTUIT_ENVIRONMENT=" in SCRIPT.read_text(encoding="utf-8")


def test_doc_distinguishes_the_three_surfaces():
    """Acceptance criterion: docs must clearly distinguish readiness checks,
    the data API, and the browser OAuth bootstrap."""
    text = DOC.read_text(encoding="utf-8")
    assert "/readyz" in text
    assert "/v1/" in text
    assert "/admin/oauth/start" in text
    # Calls out that the consuming app never holds the Intuit token.
    assert "never" in text.lower()
    assert "token" in text.lower()


@pytest.mark.parametrize("needle", ["launch token", "403", "return"])
def test_doc_explains_gate_and_return_flow(needle):
    assert needle in DOC.read_text(encoding="utf-8").lower()
