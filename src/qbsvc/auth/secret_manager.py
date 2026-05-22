from __future__ import annotations

import json
import threading
from typing import Any

from qbsvc.auth.tokens import TokenData
from qbsvc.exceptions import TokenStoreError


class SecretManagerTokenStore:
    """GCP Secret Manager-backed TokenStore.

    Persists the QBO token blob as JSON. Each `save()` writes a new secret
    version — refresh-token rotation never overwrites or deletes prior
    versions, which preserves the recovery path if a rotated token is
    rejected by Intuit.

    Exposes `refresh_lock` (a `threading.Lock`) so the synchronous
    `QBClient` refresh path can serialize concurrent refreshes. Two
    concurrent refreshes against the same realm would otherwise race:
    the first invalidates the refresh token Intuit issued, and the
    second fails on a now-stale value. FastAPI runs sync route handlers
    in a threadpool, so `threading.Lock` is the right primitive for the
    current sync client.
    """

    def __init__(
        self,
        project_id: str,
        secret_name: str,
        *,
        client: Any,
    ):
        if not project_id:
            raise ValueError("project_id is required")
        if not secret_name:
            raise ValueError("secret_name is required")
        if client is None:
            raise ValueError("client is required")
        self._project = project_id
        self._secret = secret_name
        self._client = client
        self._lock = threading.Lock()

    @property
    def refresh_lock(self) -> "threading.Lock":
        """Lock the synchronous refresh path holds across load → refresh →
        save. Memoized at the deps layer so every per-request QBClient
        injection shares the same lock instance.
        """
        return self._lock

    def load(self) -> TokenData | None:
        from google.api_core import exceptions as gax_exc

        name = f"projects/{self._project}/secrets/{self._secret}/versions/latest"
        try:
            response = self._client.access_secret_version(request={"name": name})
        except (gax_exc.NotFound, gax_exc.FailedPrecondition):
            return None
        except gax_exc.PermissionDenied as exc:
            raise TokenStoreError(
                f"Permission denied reading {name}: "
                "confirm the runtime service account has "
                "roles/secretmanager.secretAccessor on this secret."
            ) from exc

        try:
            data = json.loads(response.payload.data.decode("utf-8"))
            return TokenData.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
            return None

    def save(self, tokens: TokenData) -> None:
        from google.api_core import exceptions as gax_exc

        parent = f"projects/{self._project}/secrets/{self._secret}"
        payload = json.dumps(tokens.to_dict()).encode("utf-8")
        try:
            self._client.add_secret_version(
                request={"parent": parent, "payload": {"data": payload}}
            )
        except gax_exc.GoogleAPICallError as exc:
            # Intuit has already rotated the refresh token on its side. If we
            # cannot persist it, the next cold start dies on a now-invalid
            # token — surface the failure loudly instead of swallowing it.
            raise TokenStoreError(
                f"Failed to persist rotated token to {parent}: {exc}"
            ) from exc
