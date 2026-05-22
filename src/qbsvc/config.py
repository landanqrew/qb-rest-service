from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings. Loaded once per process."""

    model_config = SettingsConfigDict(env_prefix="QBSVC_", env_file=".env", extra="ignore")

    intuit_client_id: str = Field(default="")
    intuit_client_secret: str = Field(default="")
    intuit_environment: str = Field(default="production", pattern="^(production|sandbox)$")

    realm_id: str = Field(default="")

    token_backend: str = Field(default="file", pattern="^(file|secret_manager)$")
    gcp_project: str = Field(default="")
    secret_name_tokens: str = Field(default="mwl-qb-tokens")
    secret_name_client: str = Field(default="mwl-qb-client")

    # Full URL Intuit redirects to after consent. Must be registered in the
    # Intuit developer console and match what /admin/oauth/start sends.
    oauth_redirect_uri: str = Field(default="")
    oauth_state_ttl_seconds: int = Field(default=600, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
