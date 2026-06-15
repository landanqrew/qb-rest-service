from __future__ import annotations

from pathlib import Path

from qbsvc.auth.tokens import FileTokenStore, TokenData, TokenStore


def _sample() -> TokenData:
    return TokenData(
        access_token="at",
        refresh_token="rt",
        realm_id="123456789",
        expires_at=1_700_000_000.0,
    )


def test_file_token_store_round_trip(tmp_path: Path):
    store = FileTokenStore(path=tmp_path / "tokens.json")
    assert store.load() is None

    tokens = _sample()
    store.save(tokens)

    loaded = store.load()
    assert loaded == tokens


def test_file_token_store_creates_parent_dir(tmp_path: Path):
    nested = tmp_path / "nested" / "deeper" / "tokens.json"
    store = FileTokenStore(path=nested)
    store.save(_sample())
    assert nested.exists()


def test_file_token_store_chmod_0600(tmp_path: Path):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path=path)
    store.save(_sample())
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_file_token_store_corrupt_file_returns_none(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text("not json")
    store = FileTokenStore(path=path)
    assert store.load() is None


def test_file_token_store_missing_keys_returns_none(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text('{"access_token": "at"}')
    store = FileTokenStore(path=path)
    assert store.load() is None


def test_file_token_store_satisfies_protocol(tmp_path: Path):
    store = FileTokenStore(path=tmp_path / "tokens.json")
    assert isinstance(store, TokenStore)
