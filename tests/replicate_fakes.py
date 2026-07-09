"""Shared test double for the replicator suites.

`_FakeRealm` stands in for one QBO company behind an httpx.MockTransport: it
answers `query` reads from seeded rows and records `POST` creates, minting new
sandbox Ids so tests can prove cross-realm ID remapping happens end to end.
Importable via the repo-root pythonpath entry (pyproject `pythonpath = [..., "."]`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings


def entity_from_query(sql: str) -> str:
    # "SELECT * FROM Item WHERE ... STARTPOSITION 1 MAXRESULTS 1000"
    parts = sql.split()
    return parts[parts.index("FROM") + 1]


class FakeRealm:
    """In-memory QBO company: seeded read rows + recorded creates."""

    def __init__(
        self,
        seed: dict[str, list[dict]] | None = None,
        *,
        fail_on: dict[str, dict] | None = None,
    ):
        self.rows: dict[str, list[dict]] = {k: list(v) for k, v in (seed or {}).items()}
        self.created: dict[str, list[dict]] = {}
        # entity -> {"code","message","intuit_tid"}: make creates of that entity
        # return a QBO Fault, so tests can exercise the diagnostic-capture path.
        self.fail_on = fail_on or {}
        self._next_id = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path.endswith("/query"):
            qs = parse_qs(urlparse(str(request.url)).query)
            entity = entity_from_query(qs["query"][0])
            return httpx.Response(
                200, json={"QueryResponse": {entity: self.rows.get(entity, [])}}
            )
        entity = path.rsplit("/", 1)[-1].capitalize()
        if entity in self.fail_on:
            spec = self.fail_on[entity]
            headers = {"intuit_tid": spec.get("intuit_tid", "tid-test")}
            return httpx.Response(
                400,
                headers=headers,
                json={
                    "Fault": {
                        "type": "ValidationFault",
                        "Error": [
                            {
                                "code": spec.get("code", "6240"),
                                "Message": spec.get("message", "rejected"),
                                "Detail": spec.get("detail", ""),
                            }
                        ],
                    }
                },
            )
        body = json.loads(request.content)
        self.created.setdefault(entity, []).append(body)
        new_id = str(self._next_id)
        self._next_id += 1
        return httpx.Response(200, json={entity: {**body, "Id": new_id}})


def client_for(realm: FakeRealm, tmp_path: Path, name: str) -> QBClient:
    """A QBClient wired to `realm` via MockTransport, tokens in tmp_path."""
    store = FileTokenStore(path=tmp_path / f"{name}.json")
    store.save(
        TokenData(
            access_token="a",
            refresh_token="r",
            realm_id=f"realm-{name}",
            expires_at=time.time() + 3600,
        )
    )
    client = QBClient(token_store=store, settings=Settings())
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(realm.handler), timeout=30)
    return client
