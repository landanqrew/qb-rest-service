from __future__ import annotations

import secrets
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Event
from urllib.parse import urlencode, parse_qs, urlparse

import httpx

from qbsvc.auth.discovery import get_discovery
from qbsvc.auth.tokens import TokenData, TokenStore
from qbsvc.config import Settings
from qbsvc.exceptions import AuthError, TokenRefreshError

REDIRECT_PORT = 8418
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "com.intuit.quickbooks.accounting"


def build_authorize_url(
    client_id: str, state: str, redirect_uri: str, authorize_endpoint: str
) -> str:
    """Build the Intuit consent-screen URL the operator's browser is sent to.

    `authorize_endpoint` comes from the discovery document (see
    qbsvc.auth.discovery) so the flow tracks Intuit's published endpoint.
    """
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{authorize_endpoint}?{urlencode(params)}"


def exchange_code(
    settings: Settings, code: str, realm_id: str, redirect_uri: str
) -> TokenData:
    """Exchange an Intuit authorization code for tokens.

    The `redirect_uri` must match exactly what was sent to `/connect/oauth2`
    in the authorize step — Intuit rejects the exchange otherwise.
    """
    _require_creds(settings)

    resp = httpx.post(
        get_discovery(settings).token_endpoint,
        auth=(settings.intuit_client_id, settings.intuit_client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )

    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed ({resp.status_code}): {resp.text}")

    return _parse_token_response(resp.json(), realm_id)


def revoke(settings: Settings, token: str) -> None:
    """Revoke a token at Intuit's revocation endpoint.

    Pass the refresh token: revoking it invalidates the whole grant (the
    paired access token is revoked with it). The endpoint comes from the
    discovery document (see qbsvc.auth.discovery), and auth is HTTP Basic
    with the app's client credentials — same as the token exchange.

    Raises AuthError on any non-200 so callers can decide whether to tear
    down local state (the disconnect route keeps the token on failure).
    """
    _require_creds(settings)

    resp = httpx.post(
        get_discovery(settings).revocation_endpoint,
        auth=(settings.intuit_client_id, settings.intuit_client_secret),
        json={"token": token},
    )

    if resp.status_code != 200:
        raise AuthError(f"Token revoke failed ({resp.status_code}): {resp.text}")


def _require_creds(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("intuit_client_id", settings.intuit_client_id),
            ("intuit_client_secret", settings.intuit_client_secret),
        )
        if not value
    ]
    if missing:
        raise AuthError(f"Missing Intuit credentials: {', '.join(missing)}")


def login(settings: Settings, store: TokenStore) -> TokenData:
    """Run the full OAuth authorization code flow for Intuit.

    Note: the localhost-callback flow here is the dev/CLI path. Production
    OAuth lands via FastAPI routes (issue #3).
    """
    _require_creds(settings)

    state = secrets.token_urlsafe(32)
    auth_code: str | None = None
    realm_id: str | None = None
    error_message: str | None = None
    done = Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code, realm_id, error_message
            params = parse_qs(urlparse(self.path).query)

            if params.get("state", [None])[0] != state:
                error_message = "State mismatch — possible CSRF attack."
                self._respond(400, "State mismatch. Please try again.")
            elif "error" in params:
                error_message = params["error"][0]
                self._respond(400, f"Authorization failed: {error_message}")
            elif "code" in params:
                auth_code = params["code"][0]
                realm_id = params.get("realmId", [None])[0]
                self._respond(200, "Authorization successful! You can close this tab.")
            else:
                error_message = "No authorization code received."
                self._respond(400, "Missing authorization code.")

            done.set()

        def _respond(self, status: int, body: str):
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = f"<html><body><h2>{body}</h2></body></html>"
            self.wfile.write(html.encode())

        def log_message(self, format, *args):
            pass

    auth_url = build_authorize_url(
        client_id=settings.intuit_client_id,
        state=state,
        redirect_uri=REDIRECT_URI,
        authorize_endpoint=get_discovery(settings).authorization_endpoint,
    )

    server = HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)
    server.timeout = 120

    webbrowser.open(auth_url)

    while not done.is_set():
        server.handle_request()

    server.server_close()

    if error_message:
        raise AuthError(f"OAuth failed: {error_message}")
    if not auth_code:
        raise AuthError("No authorization code received.")
    if not realm_id:
        raise AuthError("No realm ID (company ID) received from Intuit.")

    tokens = exchange_code(settings, auth_code, realm_id, REDIRECT_URI)
    store.save(tokens)
    return tokens


def refresh(
    settings: Settings,
    store: TokenStore,
    refresh_token: str,
    realm_id: str,
) -> TokenData:
    """Exchange a refresh token for new tokens and persist the rotation."""
    _require_creds(settings)

    resp = httpx.post(
        get_discovery(settings).token_endpoint,
        auth=(settings.intuit_client_id, settings.intuit_client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )

    if resp.status_code != 200:
        raise TokenRefreshError(
            f"Token refresh failed ({resp.status_code}): {resp.text}"
        )

    tokens = _parse_token_response(resp.json(), realm_id)
    store.save(tokens)
    return tokens


def _parse_token_response(data: dict, realm_id: str) -> TokenData:
    """Parse the token endpoint response into TokenData."""
    DEFAULT_EXPIRES_IN = 3600

    try:
        return TokenData(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            realm_id=realm_id,
            expires_at=time.time() + data.get("expires_in", DEFAULT_EXPIRES_IN),
        )
    except KeyError as e:
        raise AuthError(f"Unexpected token response — missing {e}") from e
