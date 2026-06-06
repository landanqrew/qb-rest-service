from __future__ import annotations

import logging
import re
import time

import httpx

from qbsvc.api.rate_limit import TokenBucket
from qbsvc.auth.oauth import refresh as oauth_refresh
from qbsvc.auth.tokens import TokenData, TokenStore
from qbsvc.config import Settings, get_settings
from qbsvc.exceptions import APIError, AuthError, NotAuthenticatedError, RateLimitError

BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
SANDBOX_BASE_URL = "https://sandbox-quickbooks.api.intuit.com/v3/company"
MINOR_VERSION = "75"

_log = logging.getLogger("qbsvc.qbo")


class QBClient:
    """Synchronous REST client for QuickBooks Online with auth and rate-limit handling.

    Sync-with-threadpool over async on purpose: the QBO surface is one outbound
    call per inbound request, the existing oauth.refresh path is sync, and
    FastAPI already runs sync dependencies in a threadpool. Going async would
    fork httpx into Client/AsyncClient pairs across the auth module for no
    win in throughput here.
    """

    def __init__(
        self,
        token_store: TokenStore,
        settings: Settings | None = None,
        rate_limiter: TokenBucket | None = None,
    ):
        self._store = token_store
        self._settings = settings or get_settings()
        self._tokens: TokenData | None = None
        self._http = httpx.Client(timeout=30)
        # Sandbox realms live on a separate API host; the OAuth token
        # endpoint is shared, so only the entity base URL switches.
        self._base_url = (
            SANDBOX_BASE_URL
            if self._settings.intuit_environment == "sandbox"
            else BASE_URL
        )
        # When no shared bucket is injected, fall back to a per-instance one
        # sized off settings. Tests rely on this so they don't have to wire
        # one up every time; deps.py injects a process-wide bucket in prod
        # so the limit actually constrains concurrent FastAPI requests.
        self._bucket = rate_limiter or TokenBucket(
            rate_per_sec=self._settings.rate_limit_per_min / 60.0,
            capacity=self._settings.rate_limit_burst,
        )

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

        # Look up the entity by the name parsed from `FROM <Entity>` rather
        # than returning whatever list appears first in QueryResponse —
        # protects callers if QBO ever surfaces a sibling list (warnings,
        # etc.) in front of the entity rows.
        entity = _parse_from_entity(sql)
        if entity is None:
            return []

        # QBO response keys are always PascalCase (e.g. "Customer"). Match
        # case-insensitively so a lowercase `from customer` in the SQL still
        # resolves to the right list instead of silently returning [].
        target = entity.lower()
        for key, value in query_response.items():
            if key.lower() == target and isinstance(value, list):
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
        if not self._bucket.try_acquire():
            # Local fail-fast: we trip our own bucket before QBO's 500/min
            # ceiling would. Log so operators can see when load is hitting
            # the cap, and surface retry_after so the HTTP layer can return
            # 429 with a Retry-After header.
            _log.warning(
                "qbo_rate_limited_local",
                extra={
                    "method": method,
                    "qbo_endpoint": endpoint,
                },
            )
            raise RateLimitError(
                "Local rate limit exceeded.",
                retry_after=self._bucket.retry_after_seconds,
            )

        tokens = self._ensure_tokens()
        url = f"{self._base_url}/{tokens.realm_id}/{endpoint}"

        params = dict(params or {})
        params["minorversion"] = MINOR_VERSION

        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "Accept": "application/json",
        }

        start = time.perf_counter()
        retries = 0
        resp = self._http.request(method, url, headers=headers, params=params, json=json_body)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            time.sleep(retry_after)
            retries += 1
            resp = self._http.request(method, url, headers=headers, params=params, json=json_body)
            if resp.status_code == 429:
                self._log_call(
                    method, endpoint, resp.status_code, start, tokens.realm_id, retries
                )
                raise RateLimitError("Rate limited after retry. Try again later.")

        if resp.status_code == 401:
            try:
                tokens = self._refresh_tokens()
            except Exception:
                # Capture the original 401 in the structured log even when
                # the refresh exchange itself blows up — otherwise the QBO
                # call goes missing from observability while the auth log
                # records the refresh failure separately.
                self._log_call(
                    method, endpoint, 401, start, tokens.realm_id, retries
                )
                raise
            headers["Authorization"] = f"Bearer {tokens.access_token}"
            retries += 1
            resp = self._http.request(method, url, headers=headers, params=params, json=json_body)

        self._log_call(method, endpoint, resp.status_code, start, tokens.realm_id, retries)

        if resp.status_code >= 400:
            self._raise_api_error(resp)

        return resp.json()

    @staticmethod
    def _log_call(
        method: str,
        endpoint: str,
        status: int,
        start: float,
        realm_id: str,
        retries: int,
    ) -> None:
        _log.info(
            "qbo_call",
            extra={
                "method": method,
                "qbo_endpoint": endpoint,
                "status": status,
                "qbo_duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "realm_id": realm_id,
                "retries": retries,
            },
        )

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
        # Snapshot before the lock so the in-critical-section fallback
        # below can never deref a None self._tokens (e.g., if some future
        # code path nulls the cache while we're waiting for the lock).
        cached = self._tokens
        if cached is None:
            raise AuthError("No tokens to refresh.")
        # Serialize concurrent refreshes against the same store. Without
        # this, two FastAPI threads can both call Intuit, and the loser
        # ends up with an invalidated refresh token (Intuit rotates on
        # every successful refresh).
        with self._store.refresh_lock:
            # Another caller may have refreshed while we waited for the
            # lock — check the store before hitting Intuit a second time.
            # The acquire/release of threading.Lock provides the memory
            # barrier needed to see the winner's save.
            stored = self._store.load()
            if stored is not None and not stored.is_expired:
                self._tokens = stored
                return stored
            # Pull the refresh token from the most-recent persisted copy.
            # If another process rotated it between our initial load and
            # the lock acquisition, our cached refresh_token is already
            # invalid; prefer `stored` when present. `cached` is non-None
            # (guarded above), so `source` is too.
            source: TokenData = stored if stored is not None else cached
            new_tokens = oauth_refresh(
                self._settings,
                self._store,
                source.refresh_token,
                source.realm_id,
            )
            self._tokens = new_tokens
            return new_tokens

    def _raise_api_error(self, resp: httpx.Response) -> None:
        # Keep the parsed body on the exception so callers can inspect Fault
        # codes (e.g. 6240 for duplicate DocNumber) without re-parsing or
        # losing structure to the flattened `detail` string.
        body: dict | None = None
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = None

        if body is not None:
            fault = body.get("Fault", {})
            errors = fault.get("Error", [])
            detail = "; ".join(e.get("Detail", e.get("Message", str(e))) for e in errors)
        else:
            detail = resp.text

        raise APIError(resp.status_code, detail, raw=body)


_FROM_ENTITY_RE = re.compile(r"\bFROM\s+([A-Za-z]\w*)", re.IGNORECASE)


def _parse_from_entity(sql: str) -> str | None:
    """Pull the entity name out of `... FROM <Entity> ...`. Whitespace/case
    tolerant. Returns None if no FROM clause can be located so callers can
    fall back to an empty result rather than raise."""
    match = _FROM_ENTITY_RE.search(sql)
    return match.group(1) if match else None
