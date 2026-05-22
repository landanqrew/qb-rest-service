from __future__ import annotations

from functools import lru_cache
from typing import Iterator

from fastapi import Depends

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenStore
from qbsvc.config import Settings, get_settings


@lru_cache
def _file_token_store() -> FileTokenStore:
    return FileTokenStore()


def get_token_store(settings: Settings = Depends(get_settings)) -> TokenStore:
    """Resolve the TokenStore configured via QBSVC_TOKEN_BACKEND."""
    if settings.token_backend == "file":
        return _file_token_store()
    if settings.token_backend == "secret_manager":
        raise NotImplementedError(
            "SecretManagerTokenStore arrives in issue #2."
        )
    raise ValueError(f"Unknown token_backend: {settings.token_backend!r}")


def get_qb_client(
    token_store: TokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> Iterator[QBClient]:
    client = QBClient(token_store=token_store, settings=settings)
    try:
        yield client
    finally:
        client.close()
