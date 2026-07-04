"""Deploy-contract invariants for the qb-service DATA API (#52).

Issue #51 (merged in #53) added the route-surface toggles and the public
`qb-admin` bootstrap service, but the qb-service deploy artifacts were never
wired to actually turn the admin surface OFF. This locks the other half of the
split: `qb-service` must deploy IAM-locked, serve the data API only, keep the
browser OAuth admin routes disabled, and stop telling operators to browse the
IAM-locked `/admin/oauth/start` (which cannot work — a browser can't attach a
Cloud Run identity token). OAuth bootstrap belongs to `qb-admin`.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY = REPO_ROOT / "deploy"

SCRIPT = DEPLOY / "deploy.sh"
YAML = DEPLOY / "cloud-run.yaml"


def _script_without_no_allow(text: str) -> str:
    """Strip the `--no-allow-unauthenticated` flag so a substring check for the
    public `--allow-unauthenticated` flag can't be fooled by the locked one."""
    return text.replace("--no-allow-unauthenticated", "")


def test_deploy_artifacts_exist():
    assert SCRIPT.is_file()
    assert YAML.is_file()


def test_deploy_script_is_iam_locked_and_admin_routes_off():
    text = SCRIPT.read_text(encoding="utf-8")
    # IAM-locked: the data service must not be publicly reachable.
    assert "--no-allow-unauthenticated" in text
    assert "--allow-unauthenticated" not in _script_without_no_allow(text)
    # Admin OAuth surface OFF on the data service.
    assert "QBSVC_ENABLE_ADMIN_ROUTES=false" in text
    # Data routes stay ON (default true) — never disabled here.
    assert "QBSVC_ENABLE_DATA_ROUTES=false" not in text


def test_deploy_script_does_not_direct_operators_to_locked_oauth_start():
    """Issue #52 item 5: the old next-steps output told operators to browse
    `${URL}/admin/oauth/start` on the IAM-locked service, which cannot work.
    OAuth bootstrap now lives on qb-admin; deploy.sh must point there instead.

    Referencing `/admin/oauth/start` on the qb-admin URL is fine (that's the
    acceptance-criteria QB_OAUTH_START_URL); what's banned is building the start
    URL from *this* service's own ${URL} and the old "admin identity" framing."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "${URL}/admin/oauth/start" not in text
    assert "admin identity" not in text
    # Points operators at the browser-safe bootstrap service for OAuth, and
    # surfaces the two-URL Sample Manager config from the acceptance criteria.
    assert "qb-admin" in text
    assert "QB_OAUTH_START_URL=" in text


def test_deploy_yaml_is_data_only():
    text = YAML.read_text(encoding="utf-8")
    # Admin routes disabled, scoped to the same env entry as the name so an
    # unrelated `value: "false"` elsewhere can't satisfy the check.
    assert (
        'name: QBSVC_ENABLE_ADMIN_ROUTES\n              value: "false"' in text
    )
    # The data-only manifest must not declare the OAuth callback redirect URI
    # env var — that surface is served by qb-admin, not qb-service. (A comment
    # explaining its absence is fine; the `name:` declaration is what's banned.)
    assert "name: QBSVC_OAUTH_REDIRECT_URI" not in text
