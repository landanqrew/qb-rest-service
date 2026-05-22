from __future__ import annotations

import time

import httpx

from qbsvc.auth.oauth import refresh
from qbsvc.auth.token_store import TokenData, load_tokens
from qbsvc.config import Config
from qbsvc.exceptions import AuthError, APIError, RateLimitError

BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
MINOR_VERSION = "75"


class QBClient:
    """Synchronous REST client for QuickBooks Online with auth and rate-limit handling."""

    def __init__(self, config: Config | None = None, company: str | None = None):
        self.config = config or Config.load()
        self._company = self.config.resolve_company(company)
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

        # QBO wraps query results in QueryResponse
        query_response = resp.get("QueryResponse", {})

        # The entity key varies — find the first list value
        for value in query_response.values():
            if isinstance(value, list):
                return value

        return []

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

        # Handle 429 with a single retry
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            time.sleep(retry_after)
            resp = self._http.request(method, url, headers=headers, params=params, json=json_body)
            if resp.status_code == 429:
                raise RateLimitError("Rate limited after retry. Try again later.")

        if resp.status_code == 401:
            # Token may have just expired — try one refresh
            tokens = self._refresh_tokens()
            headers["Authorization"] = f"Bearer {tokens.access_token}"
            resp = self._http.request(method, url, headers=headers, params=params, json=json_body)

        if resp.status_code >= 400:
            self._raise_api_error(resp)

        return resp.json()

    def _ensure_tokens(self) -> TokenData:
        """Load tokens, refreshing if expired."""
        if self._tokens is None:
            self._tokens = load_tokens(self._company)

        if self._tokens is None:
            raise AuthError(
                f"Not authenticated for company '{self._company}'. "
                f"Run `qb auth login --alias {self._company}`."
            )

        if self._tokens.is_expired:
            self._tokens = self._refresh_tokens()

        return self._tokens

    def _refresh_tokens(self) -> TokenData:
        if self._tokens is None:
            raise AuthError("No tokens to refresh.")
        return refresh(
            self.config,
            self._company,
            self._tokens.refresh_token,
            self._tokens.realm_id,
        )

    def _raise_api_error(self, resp: httpx.Response) -> None:
        try:
            body = resp.json()
            fault = body.get("Fault", {})
            errors = fault.get("Error", [])
            detail = "; ".join(e.get("Detail", e.get("Message", str(e))) for e in errors)
        except Exception:
            detail = resp.text

        raise APIError(resp.status_code, detail)
