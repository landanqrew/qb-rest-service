"""Tests for the qb-pages companion public static-pages service (issue #36).

qb-pages is a deliberately tiny Cloud Run service whose *only* job is to host
the three public URLs Intuit requires for a production app review: a landing
page, an EULA, and a privacy policy. The hard constraint (scope doc Amendment 2)
is that its blast radius stays exactly three HTML files — no QBO access, no
secrets, no service-account permissions, no network path to qb-service.

Two layers of testing:

1. Structure / safety invariants — static assertions over the ``web/`` tree and
   the deploy config. These guard the "blast radius is three HTML files"
   constraint and the acceptance-criteria content requirements.

2. A live serving test that runs the *actual* nginx config from the repo and
   curls ``/``, ``/eula``, ``/privacy`` (+ ``/healthz``) for 200s. The
   acceptance criterion is "docker run -> curl 200s"; the container just wraps
   this same nginx config, so running nginx directly is a faithful proxy and
   works in CI where the container registry CDN is unreachable. Skips cleanly
   if no ``nginx`` binary is present.
"""

from __future__ import annotations

import http.client
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"
DEPLOY = REPO_ROOT / "deploy"

PAGES = {
    "/": WEB / "index.html",
    "/eula": WEB / "eula.html",
    "/privacy": WEB / "privacy.html",
}


# --------------------------------------------------------------------------- #
# Layer 1: structure / safety invariants
# --------------------------------------------------------------------------- #


def test_web_dir_holds_exactly_the_three_pages_and_config():
    """Blast radius: the public surface is three HTML files, nothing more."""
    assert WEB.is_dir(), "web/ directory must exist"
    html_files = sorted(p.name for p in WEB.glob("*.html"))
    assert html_files == ["eula.html", "index.html", "privacy.html"]


@pytest.mark.parametrize("page", ["index.html", "eula.html", "privacy.html"])
def test_pages_exist_and_are_nonempty(page):
    path = WEB / page
    assert path.is_file(), f"{page} must exist"
    assert path.stat().st_size > 0, f"{page} must not be empty"


def test_landing_page_names_app_and_purpose():
    """Intuit reviewers must see a real page, not a placeholder."""
    html = (WEB / "index.html").read_text(encoding="utf-8").lower()
    # Names the app and the integration it describes.
    assert "lab intake" in html
    assert "quickbooks" in html
    # Links to both required legal pages.
    assert "/eula" in html
    assert "/privacy" in html
    # Not a placeholder.
    assert "lorem ipsum" not in html
    assert "placeholder" not in html


@pytest.mark.parametrize("page", ["eula.html", "privacy.html"])
def test_legal_pages_marked_draft_for_review(page):
    """EULA + privacy drafts must be clearly marked for human review."""
    html = (WEB / page).read_text(encoding="utf-8").upper()
    assert "DRAFT" in html


def test_privacy_page_describes_data_handling():
    html = (WEB / "privacy.html").read_text(encoding="utf-8").lower()
    assert "privacy" in html
    assert "quickbooks" in html


def _all_web_text() -> str:
    chunks = []
    for path in WEB.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def test_web_tree_has_no_secrets_or_qb_service_references():
    """Hard constraint: no secrets, no env, no network path to qb-service."""
    blob = _all_web_text().lower()
    forbidden = [
        "qbsvc_",            # qb-service env var prefix
        "client_secret",
        "secret_manager",
        "mwl-qb-tokens",
        "mwl-qb-client",
        "refresh_token",
        "access_token",
        "run.app/admin",     # no link into qb-service admin surface
    ]
    for needle in forbidden:
        assert needle not in blob, f"web/ must not reference {needle!r}"


def test_web_tree_has_no_javascript():
    """Plain HTML/CSS only — no JS frameworks, small and boring."""
    assert not list(WEB.rglob("*.js"))
    blob = _all_web_text().lower()
    assert "<script" not in blob


# --------------------------------------------------------------------------- #
# Layer 1b: container + deploy config invariants
# --------------------------------------------------------------------------- #


def test_web_dockerfile_uses_static_server_and_no_secrets():
    dockerfile = WEB / "Dockerfile"
    assert dockerfile.is_file(), "web/Dockerfile must exist"
    text = dockerfile.read_text(encoding="utf-8")
    lower = text.lower()
    # A static file server base (nginx or caddy).
    assert "nginx" in lower or "caddy" in lower
    # Honors Cloud Run's $PORT.
    assert "PORT" in text
    # No secret/env plumbing baked into the image.
    assert "secret" not in lower
    assert "qbsvc_" not in lower


def test_nginx_template_serves_clean_urls():
    conf = WEB / "nginx" / "default.conf.template"
    assert conf.is_file(), "web/nginx/default.conf.template must exist"
    text = conf.read_text(encoding="utf-8")
    assert "${PORT}" in text  # rendered by nginx envsubst at startup
    for route in ("/eula", "/privacy"):
        assert route in text


def test_nginx_template_listens_on_ipv6():
    """Cloud Run gen2 may route over IPv6; the server must bind both stacks."""
    text = (WEB / "nginx" / "default.conf.template").read_text(encoding="utf-8")
    assert "listen       ${PORT};" in text or "listen ${PORT};" in text
    assert "[::]:${PORT}" in text


def test_nginx_template_sets_security_headers():
    text = (WEB / "nginx" / "default.conf.template").read_text(encoding="utf-8")
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Content-Security-Policy",
    ):
        assert header in text


def test_qb_pages_cloud_run_yaml_is_locked_down():
    yaml = DEPLOY / "qb-pages.cloud-run.yaml"
    assert yaml.is_file(), "deploy/qb-pages.cloud-run.yaml must exist"
    text = yaml.read_text(encoding="utf-8")
    lower = text.lower()
    assert "qb-pages" in lower
    # Dedicated, distinct service account (not the qb-service runtime SA).
    assert "qb-pages" in text and "serviceAccountName" in text
    assert "qb-service-runtime@" not in text
    # No secrets, no QBO env wiring.
    assert "secretkeyref" not in lower
    assert "qbsvc_" not in lower


def test_qb_pages_deploy_script_is_unauthenticated_with_dedicated_sa():
    script = DEPLOY / "qb-pages.deploy.sh"
    assert script.is_file(), "deploy/qb-pages.deploy.sh must exist"
    text = script.read_text(encoding="utf-8")
    assert "--allow-unauthenticated" in text
    assert "qb-pages" in text
    # Must not wire any secrets or grant IAM to the page service.
    assert "--set-secrets" not in text
    assert "secretmanager" not in text.lower()


def test_deploy_docs_mention_qb_pages():
    """README or a deploy doc must document the qb-pages deploy step."""
    candidates = [
        REPO_ROOT / "README.md",
        DEPLOY / "qb-pages-setup.md",
    ]
    found = any(
        p.is_file() and "qb-pages" in p.read_text(encoding="utf-8")
        for p in candidates
    )
    assert found, "qb-pages deploy step must be documented in README or deploy docs"


# --------------------------------------------------------------------------- #
# Layer 2: live serving via the real nginx config
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _render_server_conf(template: str, port: int, root: Path) -> str:
    """Render the nginx server block the way the container's envsubst would.

    The container listens on IPv4+IPv6 across all interfaces via ${PORT}; the
    test only needs a single IPv4 loopback socket on an ephemeral port. So we
    drop the IPv6 listener, bind the IPv4 one to 127.0.0.1, fill in ${PORT}, and
    repoint the docroot at a local directory (no /usr/share/nginx/html needed).
    """
    rendered = re.sub(r"\n[ \t]*listen[ \t]+\[::\]:\$\{PORT\};", "", template)
    rendered = re.sub(r"listen[ \t]+\$\{PORT\}", f"listen 127.0.0.1:{port}", rendered)
    rendered = rendered.replace("${PORT}", str(port))
    rendered = rendered.replace("/usr/share/nginx/html", str(root))
    return rendered


@pytest.fixture()
def nginx_server(tmp_path):
    nginx_bin = shutil.which("nginx")
    if not nginx_bin:
        pytest.skip("nginx binary not available")

    template_path = WEB / "nginx" / "default.conf.template"
    if not template_path.is_file():
        pytest.skip("nginx template not present yet")

    port = _free_port()

    # Mirror the container docroot.
    docroot = tmp_path / "html"
    docroot.mkdir()
    for page in ("index.html", "eula.html", "privacy.html"):
        shutil.copy(WEB / page, docroot / page)

    server_conf = _render_server_conf(
        template_path.read_text(encoding="utf-8"), port, docroot
    )
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "default.conf").write_text(server_conf, encoding="utf-8")

    mime_types = "/etc/nginx/mime.types"
    mime_include = (
        f"    include {mime_types};\n" if os.path.exists(mime_types) else ""
    )
    # When the master runs as root, workers default to an unprivileged user that
    # can't traverse pytest's mode-700 tmp base. Pin workers to root so they can
    # read the docroot. (Ignored by nginx when not run as root, where the
    # launching user already owns tmp_path.)
    user_directive = "user root;\n" if os.geteuid() == 0 else ""
    master = f"""
{user_directive}worker_processes 1;
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log warn;
events {{ worker_connections 64; }}
http {{
{mime_include}    default_type application/octet-stream;
    access_log off;
    client_body_temp_path {tmp_path}/client_body;
    proxy_temp_path {tmp_path}/proxy;
    fastcgi_temp_path {tmp_path}/fastcgi;
    uwsgi_temp_path {tmp_path}/uwsgi;
    scgi_temp_path {tmp_path}/scgi;
    include {conf_d}/default.conf;
}}
"""
    master_path = tmp_path / "nginx.conf"
    master_path.write_text(master, encoding="utf-8")

    proc = subprocess.Popen(
        [nginx_bin, "-c", str(master_path), "-p", str(tmp_path), "-g", "daemon off;"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the port to accept connections.
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ""
            pytest.fail(f"nginx exited early: {err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("nginx did not start listening in time")

    yield port

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="ignore")
        return resp.status, resp.getheader("Content-Type", ""), body
    finally:
        conn.close()


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/", "quickbooks"),
        ("/eula", "draft"),
        ("/privacy", "privacy"),
    ],
)
def test_live_pages_return_200(nginx_server, path, needle):
    status, ctype, body = _get(nginx_server, path)
    assert status == 200, f"{path} returned {status}"
    assert "text/html" in ctype, f"{path} content-type was {ctype!r}"
    assert needle in body.lower()


def test_live_healthz_ok(nginx_server):
    status, _ctype, body = _get(nginx_server, "/healthz")
    assert status == 200
    assert "ok" in body.lower()


def test_live_pages_carry_security_headers(nginx_server):
    conn = http.client.HTTPConnection("127.0.0.1", nginx_server, timeout=5)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("x-content-type-options") == "nosniff"
    assert "default-src 'none'" in headers.get("content-security-policy", "")
