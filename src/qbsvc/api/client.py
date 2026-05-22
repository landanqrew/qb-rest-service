from __future__ import annotations

import time

import httpx

from qbsvc.auth.oauth import refresh as oauth_refresh
from qbsvc.auth.tokens import TokenData, TokenStore
from qbsvc.config import Settings, get_settings
from qbsvc.exceptions import APIError, AuthError, NotAuthenticatedError, RateLimitError

BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
MINOR_VERSION = "75"


class QBClient:
    """Synchronous REST client for QuickBooks Online with auth and rate-limit handling.

    Sync-with-threadpool over async on purpose: the QBO surface is one outbound
    call per inbound request, the existing oauth.refresh path is sync, and
    FastAPI already runs sync dependencies in a threadpool. Going async would
    fork httpx into Client/AsyncClient pairs across the auth module for no
    win in throughput here.
    """

    def __init__(self, token_store: TokenStore, settings: Settings | None = None):
        self._store = token_store
        self._settings = settings or get_settings()
        self._tokens: TokenData | None = None
        self._http = httpx.Client(timeout=30)

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        """GET a QBO resource. Endpoint is relative (e.g. 'customer/123')."""
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint: str, json_body: dict, params: dict | None = None) -> dict:
        """POST to a QBO resource (create or update). Endpoint is relative (e.g. 'invoice')."""
        return self._request("POST", endpoint, params=params, json_body=json_body)

    def update(self, entity: str, body: dict, operation: str | None = None) -> dict:
        """Update an existing entity. body must include Id and SyncToken.

        Pass operation='update' for sparse semantics via the body's `sparse` flag,
        or operation='delete'/'void' for those flows.
        """
        params = {"operation": operation} if operation else None
        return self._request("POST", entity.lower(), params=params, json_body=body)

    def query(self, sql: str) -> list[dict]:
        """Execute a QBO SQL-like query and return the list of entities."""
        resp = self._request("GET", "query", params={"query": sql})

        query_response = resp.get("QueryResponse", {})

        for value in query_response.values():
            if isinstance(value, list):
                return value

        return []

    def ensure_ready(self) -> None:
        """Verify auth is usable: load tokens and refresh if expired.

        Makes at most one call to Intuit's token endpoint; never touches a
        QBO entity endpoint. Used by /readyz so that readiness reflects the
        auth path the next real request would take.
        """
        self._ensure_tokens()

    def close(self):
        self._http.close()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        tokens = self._ensure_tokens()
        url = f"{BASE_URL}/{tokens.realm_id}/{endpoint}"

        params = dict(params or {})
        params["minorversion"] = MINOR_VERSION

        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "Accept": "application/json",
        }

        resp = self._http.request(method, url, headers=headers, params=params, json=json_body)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            time.sleep(retry_after)
            resp = self._http.request(method, url, headers=headers, params=params, json=json_body)
            if resp.status_code == 429:
                raise RateLimitError("Rate limited after retry. Try again later.")

        if resp.status_code == 401:
            tokens = self._refresh_tokens()
            headers["Authorization"] = f"Bearer {tokens.access_token}"
            resp = self._http.request(method, url, headers=headers, params=params, json=json_body)

        if resp.status_code >= 400:
            self._raise_api_error(resp)

        return resp.json()

    def _ensure_tokens(self) -> TokenData:
        """Load tokens from the store, refreshing if expired."""
        if self._tokens is None:
            self._tokens = self._store.load()

        if self._tokens is None:
            raise NotAuthenticatedError(
                "Not authenticated. Run the OAuth flow at /admin/oauth/start."
            )

        if self._tokens.is_expired:
            self._tokens = self._refresh_tokens()

        return self._tokens

    def _refresh_tokens(self) -> TokenData:
        if self._tokens is None:
            raise AuthError("No tokens to refresh.")
        new_tokens = oauth_refresh(
            self._settings,
            self._store,
            self._tokens.refresh_token,
            self._tokens.realm_id,
        )
        self._tokens = new_tokens
        return new_tokens

    def _raise_api_error(self, resp: httpx.Response) -> None:
        try:
            body = resp.json()
            fault = body.get("Fault", {})
            errors = fault.get("Error", [])
            detail = "; ".join(e.get("Detail", e.get("Message", str(e))) for e in errors)
        except Exception:
            detail = resp.text

        raise APIError(resp.status_code, detail)
