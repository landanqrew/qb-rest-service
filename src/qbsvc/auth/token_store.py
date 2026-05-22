from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

from qbsvc.exceptions import AuthError

TOKEN_DIR = Path(user_config_dir("qb"))
TOKEN_FILE = TOKEN_DIR / "tokens.json"
KEYRING_SERVICE = "qb-cli"


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


def _keyring_key(alias: str) -> str:
    return f"oauth-tokens-{alias}"


def _load_all_file() -> dict[str, dict]:
    """Load the full token file as {alias: token_dict}."""
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all_file(data: dict[str, dict]) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2) + "\n")
    TOKEN_FILE.chmod(0o600)


def save_tokens(alias: str, tokens: TokenData) -> None:
    """Save tokens for a company alias to keyring, falling back to file."""
    payload = json.dumps(tokens.to_dict())

    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, _keyring_key(alias), payload)
        return
    except Exception:
        pass

    # File fallback
    all_tokens = _load_all_file()
    all_tokens[alias] = tokens.to_dict()
    _save_all_file(all_tokens)


def load_tokens(alias: str) -> TokenData | None:
    """Load tokens for a company alias from keyring or file."""
    try:
        import keyring

        payload = keyring.get_password(KEYRING_SERVICE, _keyring_key(alias))
        if payload:
            return TokenData.from_dict(json.loads(payload))
    except Exception:
        pass

    all_tokens = _load_all_file()
    if alias in all_tokens:
        try:
            return TokenData.from_dict(all_tokens[alias])
        except KeyError:
            pass

    return None


def clear_tokens(alias: str) -> None:
    """Remove tokens for a company alias from all stores."""
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, _keyring_key(alias))
    except Exception:
        pass

    all_tokens = _load_all_file()
    if alias in all_tokens:
        del all_tokens[alias]
        _save_all_file(all_tokens)


def list_aliases() -> list[str]:
    """Return all stored company aliases."""
    aliases: set[str] = set()

    # Check file store
    all_tokens = _load_all_file()
    aliases.update(all_tokens.keys())

    return sorted(aliases)
