from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # Comma-separated list of email identities (Google users or service
    # accounts) allowed to reach /admin/*. Empty list disables the gate so
    # local dev keeps working without a Cloud Run IAM context. See
    # qbsvc.auth.admin_gate and deploy/oauth-setup.md §"Admin gate".
    # `NoDecode` suppresses pydantic-settings' default JSON-parsing for list
    # fields so the comma-split validator below sees the raw env string.
    admin_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("admin_allowlist", mode="before")
    @classmethod
    def _split_admin_allowlist(cls, v: object) -> object:
        # Pydantic-settings hands env values in as strings; split on comma so
        # `QBSVC_ADMIN_ALLOWLIST=a@x,b@y` parses to a clean list. Trim each
        # entry and drop empties so trailing commas or stray spaces don't
        # silently create empty allowlist slots.
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
