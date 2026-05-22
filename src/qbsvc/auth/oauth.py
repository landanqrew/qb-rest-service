from __future__ import annotations

import secrets
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Event
from urllib.parse import urlencode, parse_qs, urlparse

import httpx

from qbsvc.config import Config
from qbsvc.exceptions import AuthError
from qbsvc.auth.token_store import TokenData, save_tokens

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REDIRECT_PORT = 8418
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "com.intuit.quickbooks.accounting"


def login(config: Config, alias: str) -> TokenData:
    """Run the full OAuth authorization code flow for Intuit."""
    config.require("client_id", "client_secret")

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
            pass  # suppress server logs

    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"

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

    tokens = _exchange_code(config, auth_code, realm_id)
    save_tokens(alias, tokens)
    return tokens


def refresh(config: Config, alias: str, refresh_token: str, realm_id: str) -> TokenData:
    """Exchange a refresh token for new tokens."""
    config.require("client_id", "client_secret")

    resp = httpx.post(
        TOKEN_URL,
        auth=(config.client_id, config.client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )

    if resp.status_code != 200:
        raise AuthError(f"Token refresh failed ({resp.status_code}): {resp.text}")

    tokens = _parse_token_response(resp.json(), realm_id)
    save_tokens(alias, tokens)
    return tokens


def _exchange_code(config: Config, code: str, realm_id: str) -> TokenData:
    """Exchange authorization code for tokens."""
    resp = httpx.post(
        TOKEN_URL,
        auth=(config.client_id, config.client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
    )

    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed ({resp.status_code}): {resp.text}")

    return _parse_token_response(resp.json(), realm_id)


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
