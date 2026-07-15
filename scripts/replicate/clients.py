"""Construct the two realm-bound QBClients the replicator needs.

The service is single-realm by design (one token store, one environment). The
replicator needs two: a *source* client on the production realm and a *target*
client on the sandbox realm. Each gets its own TokenStore (distinct
secret/path, so `TokenData.realm_id` differs), its own Settings (differing
`intuit_environment`, which fixes the API base URL), and its own TokenBucket
(so the two realms don't share one 500/min budget).

Two backends, chosen by the caller:
  * secret_manager — for the in-network Cloud Run Job (prod tokens live in
    Secret Manager and are read via the runtime SA).
  * file — for local dev/debugging with two token JSON files.
"""

from __future__ import annotations

from pathlib import Path

from qbsvc.api.client import QBClient
from qbsvc.api.rate_limit import TokenBucket
from qbsvc.auth.secret_manager import SecretManagerTokenStore
from qbsvc.auth.tokens import FileTokenStore, TokenStore
from qbsvc.config import Settings


def _rate_limiter(settings: Settings) -> TokenBucket:
    return TokenBucket(
        rate_per_sec=settings.rate_limit_per_min / 60.0,
        capacity=settings.rate_limit_burst,
    )


def build_client(store: TokenStore, environment: str) -> QBClient:
    """A QBClient bound to `store`'s realm on the given Intuit environment.

    `environment` is "production" or "sandbox" — it selects the QBO API host.
    Settings are constructed directly (not via the cached get_settings) so the
    two clients can hold different environments in one process.
    """
    settings = Settings(intuit_environment=environment)
    return QBClient(
        token_store=store,
        settings=settings,
        rate_limiter=_rate_limiter(settings),
    )


def secret_manager_store(project_id: str, secret_name: str) -> SecretManagerTokenStore:
    from google.cloud import secretmanager

    return SecretManagerTokenStore(
        project_id=project_id,
        secret_name=secret_name,
        client=secretmanager.SecretManagerServiceClient(),
    )


def file_store(path: Path) -> FileTokenStore:
    return FileTokenStore(path=path)
