from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_TOKEN_PATH = Path.home() / ".config" / "qbsvc" / "tokens.json"


@dataclass
class TokenData:
    access_token: str
    refresh_token: str
    realm_id: str
    expires_at: float  # unix timestamp

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60  # 60s buffer

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "realm_id": self.realm_id,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TokenData:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            realm_id=data["realm_id"],
            expires_at=data["expires_at"],
        )


@runtime_checkable
class TokenStore(Protocol):
    """Pluggable storage for the QBO refresh-token blob.

    Single-realm: the store holds one TokenData at a time.
    """

    def load(self) -> TokenData | None: ...

    def save(self, tokens: TokenData) -> None: ...


class FileTokenStore:
    """Token store backed by a JSON file on disk.

    Used for local dev; production swaps in SecretManagerTokenStore (issue #2).
    """

    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_TOKEN_PATH

    def load(self) -> TokenData | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        try:
            return TokenData.from_dict(data)
        except (KeyError, TypeError):
            return None

    def save(self, tokens: TokenData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(tokens.to_dict(), indent=2) + "\n")
        self.path.chmod(0o600)
