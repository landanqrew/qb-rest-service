from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterator

from fastapi import Depends, HTTPException, Request

from qbsvc.api.client import QBClient
from qbsvc.api.rate_limit import TokenBucket
from qbsvc.auth import admin_launch
from qbsvc.auth.admin_gate import extract_email_from_bearer
from qbsvc.auth.oauth_state import OAuthStateStore
from qbsvc.auth.secret_manager import SecretManagerTokenStore
from qbsvc.auth.tokens import FileTokenStore, TokenStore
from qbsvc.config import Settings, get_settings


@lru_cache
def _file_token_store() -> FileTokenStore:
    return FileTokenStore()


@lru_cache
def _secret_manager_token_store(
    project_id: str, secret_name: str
) -> SecretManagerTokenStore:
    return SecretManagerTokenStore(
        project_id=project_id,
        secret_name=secret_name,
        client=_build_secret_manager_client(),
    )


@lru_cache(maxsize=1)
def _build_secret_manager_client() -> Any:
    """Construct the process-wide Secret Manager client.

    Split out (and memoized) so tests can patch it without importing
    google-cloud-secret-manager, and so we never construct more than one
    client per process.
    """
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient()


def reset_token_store_cache() -> None:
    """Clear memoized token-store instances. For tests only."""
    _file_token_store.cache_clear()
    _secret_manager_token_store.cache_clear()
    _build_secret_manager_client.cache_clear()


@lru_cache
def _oauth_state_store(ttl_seconds: int) -> OAuthStateStore:
    return OAuthStateStore(ttl_seconds=ttl_seconds)


def reset_oauth_state_store_cache() -> None:
    """Clear the memoized OAuth state store. For tests only."""
    _oauth_state_store.cache_clear()


@lru_cache
def _qbo_rate_limiter(per_min: int, burst: int) -> TokenBucket:
    """Process-wide token bucket so concurrent QBClient instances share one
    bucket. Without memoization each request would get its own bucket and
    the limit wouldn't constrain anything."""
    return TokenBucket(rate_per_sec=per_min / 60.0, capacity=burst)


def reset_rate_limiter_cache() -> None:
    """Clear the memoized rate limiter. For tests only."""
    _qbo_rate_limiter.cache_clear()


def get_qbo_rate_limiter(
    settings: Settings = Depends(get_settings),
) -> TokenBucket:
    return _qbo_rate_limiter(settings.rate_limit_per_min, settings.rate_limit_burst)


def get_oauth_state_store(
    settings: Settings = Depends(get_settings),
) -> OAuthStateStore:
    """Process-wide state store so /admin/oauth/start and /callback
    share entries across requests."""
    return _oauth_state_store(settings.oauth_state_ttl_seconds)


def require_launch_authorization(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Gate browser-initiated OAuth bootstrap on a valid launch token (issue #51).

    Applied to `/admin/oauth/start` and `/admin/oauth/disconnect` — the two
    admin actions a browser initiates directly. (The callback is not gated here:
    Intuit's redirect can't carry a launch token, and it is already protected by
    the one-time CSRF `state` minted by the gated `/start`.)

    Resolution order:
      1. Launch gate inactive (no `admin_launch_secret`) → allow. Preserves the
         existing IAM/allowlist model and local dev unchanged.
      2. Valid launch token (query param `launch` or `X-QBSVC-Launch` header) →
         allow. This is the browser button path.
      3. Allowlisted Cloud Run identity token → allow. Keeps the operator's
         identity-injecting forwarder (oauth-setup.md §3) working even when the
         launch gate is enabled on the same image.
      4. Otherwise → 403.
    """
    if not settings.admin_launch_secret:
        return

    token = request.query_params.get("launch") or request.headers.get("x-qbsvc-launch")
    if admin_launch.verify_launch_token(token, settings.admin_launch_secret):
        return

    allowlist = {
        e.strip().lower() for e in settings.admin_allowlist if e and e.strip()
    }
    if allowlist:
        email = extract_email_from_bearer(request.headers.get("authorization"))
        if email and email in allowlist:
            return

    raise HTTPException(
        status_code=403,
        detail=(
            "A valid launch token is required to start the QuickBooks connect "
            "flow. Open it from the Connect QuickBooks button in your app."
        ),
    )


def get_token_store(settings: Settings = Depends(get_settings)) -> TokenStore:
    """Resolve the TokenStore configured via QBSVC_TOKEN_BACKEND."""
    if settings.token_backend == "file":
        return _file_token_store()
    if settings.token_backend == "secret_manager":
        if not settings.gcp_project:
            raise ValueError(
                "QBSVC_GCP_PROJECT is required when QBSVC_TOKEN_BACKEND=secret_manager"
            )
        return _secret_manager_token_store(
            settings.gcp_project, settings.secret_name_tokens
        )
    raise ValueError(f"Unknown token_backend: {settings.token_backend!r}")


def get_qb_client(
    token_store: TokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
    rate_limiter: TokenBucket = Depends(get_qbo_rate_limiter),
) -> Iterator[QBClient]:
    client = QBClient(
        token_store=token_store,
        settings=settings,
        rate_limiter=rate_limiter,
    )
    try:
        yield client
    finally:
        client.close()
