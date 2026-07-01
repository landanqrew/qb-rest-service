from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as gax_exc

from qbsvc.auth.secret_manager import SecretManagerTokenStore
from qbsvc.auth.tokens import TokenData, TokenStore
from qbsvc.exceptions import TokenStoreError


def _sample() -> TokenData:
    return TokenData(
        access_token="at",
        refresh_token="rt-v1",
        realm_id="123456789",
        expires_at=1_700_000_000.0,
    )


def _fake_client_with_versions(*payloads: dict) -> MagicMock:
    """Build a fake SecretManagerServiceClient where `access_secret_version`
    returns the most recently added payload. `add_secret_version` appends.
    """
    client = MagicMock()
    versions: list[bytes] = [json.dumps(p).encode("utf-8") for p in payloads]

    def access_secret_version(request=None, **_):
        if not versions:
            raise gax_exc.FailedPrecondition("no versions")
        resp = MagicMock()
        resp.payload.data = versions[-1]
        return resp

    def add_secret_version(request=None, **_):
        versions.append(request["payload"]["data"])
        resp = MagicMock()
        resp.name = f"projects/p/secrets/s/versions/{len(versions)}"
        return resp

    client.access_secret_version.side_effect = access_secret_version
    client.add_secret_version.side_effect = add_secret_version
    client._versions = versions
    return client


def _store(client: MagicMock) -> SecretManagerTokenStore:
    return SecretManagerTokenStore(
        project_id="mwl-prod",
        secret_name="mwl-qb-tokens",
        client=client,
    )


def test_satisfies_token_store_protocol():
    store = _store(_fake_client_with_versions())
    assert isinstance(store, TokenStore)


def test_load_returns_none_when_secret_has_no_versions():
    store = _store(_fake_client_with_versions())
    assert store.load() is None


def test_load_returns_none_when_secret_not_found():
    client = MagicMock()
    client.access_secret_version.side_effect = gax_exc.NotFound("missing")
    assert _store(client).load() is None


def test_load_returns_none_for_malformed_payload():
    client = MagicMock()
    resp = MagicMock()
    resp.payload.data = b"not json"
    client.access_secret_version.return_value = resp
    assert _store(client).load() is None


def test_load_returns_none_for_payload_missing_required_keys():
    client = MagicMock()
    resp = MagicMock()
    resp.payload.data = json.dumps({"access_token": "at"}).encode("utf-8")
    client.access_secret_version.return_value = resp
    assert _store(client).load() is None


def test_round_trip_save_then_load():
    client = _fake_client_with_versions()
    store = _store(client)
    tokens = _sample()

    store.save(tokens)
    loaded = store.load()

    assert loaded == tokens


def test_save_writes_payload_as_json_blob():
    client = _fake_client_with_versions()
    store = _store(client)

    store.save(_sample())

    call = client.add_secret_version.call_args
    request = call.kwargs.get("request") or call.args[0]
    assert request["parent"] == "projects/mwl-prod/secrets/mwl-qb-tokens"
    payload = json.loads(request["payload"]["data"].decode("utf-8"))
    assert payload == {
        "access_token": "at",
        "refresh_token": "rt-v1",
        "realm_id": "123456789",
        "expires_at": 1_700_000_000.0,
    }


def test_save_creates_new_version_each_call_without_deleting_old():
    """Critical: refresh-token rotation must append, never overwrite."""
    client = _fake_client_with_versions()
    store = _store(client)

    store.save(TokenData(access_token="at1", refresh_token="rt1", realm_id="r", expires_at=1.0))
    store.save(TokenData(access_token="at2", refresh_token="rt2", realm_id="r", expires_at=2.0))
    store.save(TokenData(access_token="at3", refresh_token="rt3", realm_id="r", expires_at=3.0))

    assert client.add_secret_version.call_count == 3
    assert len(client._versions) == 3
    # Latest read returns the most recent version (rotation succeeded).
    loaded = store.load()
    assert loaded is not None
    assert loaded.refresh_token == "rt3"


def test_load_reads_from_latest_version_alias():
    client = _fake_client_with_versions()
    store = _store(client)
    store.save(_sample())
    store.load()

    call = client.access_secret_version.call_args
    request = call.kwargs.get("request") or call.args[0]
    assert request["name"].endswith("/versions/latest")


def test_refresh_lock_is_threading_lock():
    """Sync QBClient runs in FastAPI's threadpool, so `refresh_lock` must
    be the threading flavour — `asyncio.Lock` would not serialize threads.
    """
    store = _store(_fake_client_with_versions())
    # `threading.Lock()` returns an instance of a private `_thread.lock`
    # class, so checking by isinstance against `threading.Lock` does not
    # work. Check the duck-typed surface instead.
    lock = store.refresh_lock
    assert hasattr(lock, "acquire") and hasattr(lock, "release")
    assert hasattr(lock, "locked")
    # asyncio.Lock would have `_loop` / `_waiters` and lack `locked()` in
    # the form the production code uses; a sanity check that we did not
    # regress to it.
    import asyncio as _asyncio

    assert not isinstance(lock, _asyncio.Lock)


def test_refresh_lock_serializes_two_threads():
    """Two threads holding refresh_lock must not overlap in the critical
    section, simulating two concurrent refresh attempts that would
    otherwise both hit Intuit and invalidate each other's refresh token.
    """
    store = _store(_fake_client_with_versions())
    overlap_observed = False
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def critical():
        nonlocal active, max_active, overlap_observed
        with store.refresh_lock:
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                if active > 1:
                    overlap_observed = True
            time.sleep(0.01)
            with state_lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(lambda _: critical(), range(3)))

    assert max_active == 1
    assert overlap_observed is False


def test_load_raises_token_store_error_on_permission_denied():
    """IAM misconfig (the most likely first-run failure on Cloud Run) must
    surface as a clear domain error, not an unhandled GoogleAPICallError.
    Silently returning None would mask the bug and lead operators into a
    futile re-auth flow.
    """
    client = MagicMock()
    client.access_secret_version.side_effect = gax_exc.PermissionDenied("nope")
    with pytest.raises(TokenStoreError, match="Permission denied"):
        _store(client).load()


def test_save_raises_token_store_error_when_gcp_call_fails():
    """If add_secret_version fails after Intuit has rotated the refresh
    token, the rotated value is irretrievably lost. Surface loudly.
    """
    client = MagicMock()
    client.add_secret_version.side_effect = gax_exc.ServiceUnavailable("503")
    with pytest.raises(TokenStoreError, match="Failed to persist rotated token"):
        _store(client).save(_sample())


def test_save_preserves_original_gcp_exception_as_cause():
    client = MagicMock()
    underlying = gax_exc.PermissionDenied("denied")
    client.add_secret_version.side_effect = underlying
    with pytest.raises(TokenStoreError) as excinfo:
        _store(client).save(_sample())
    assert excinfo.value.__cause__ is underlying


def test_clear_makes_load_return_none_via_tombstone_version():
    """Disconnect must leave the store reading as not-authenticated. Since the
    backend never deletes versions, clear() appends a tombstone version that
    load() parses back to None.
    """
    client = _fake_client_with_versions()
    store = _store(client)
    store.save(_sample())
    assert store.load() is not None

    store.clear()

    assert store.load() is None


def test_clear_appends_a_version_without_deleting_history():
    """The tombstone must be a new version, not a destructive delete — the
    prior token versions stay in history for audit/recovery.
    """
    client = _fake_client_with_versions()
    store = _store(client)
    store.save(_sample())

    store.clear()

    # save (1) + clear (2) → two appended versions, none removed.
    assert client.add_secret_version.call_count == 2
    assert len(client._versions) == 2


def test_clear_raises_token_store_error_when_gcp_call_fails():
    client = MagicMock()
    client.add_secret_version.side_effect = gax_exc.ServiceUnavailable("503")
    with pytest.raises(TokenStoreError):
        _store(client).clear()


def test_constructor_rejects_missing_required_args():
    client = MagicMock()
    with pytest.raises(ValueError, match="project_id"):
        SecretManagerTokenStore(project_id="", secret_name="s", client=client)
    with pytest.raises(ValueError, match="secret_name"):
        SecretManagerTokenStore(project_id="p", secret_name="", client=client)
    with pytest.raises(ValueError, match="client"):
        SecretManagerTokenStore(project_id="p", secret_name="s", client=None)
