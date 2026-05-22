from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterator

from fastapi import Depends

from qbsvc.api.client import QBClient
from qbsvc.api.rate_limit import TokenBucket
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
