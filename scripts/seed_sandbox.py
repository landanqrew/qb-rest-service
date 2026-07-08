"""Seed the QBO sandbox realm with Customers and Items copied from production.

One-off developer script. Not part of the runtime service.

Prerequisites
-------------
1. Prod tokens live at ``~/.config/qbsvc/tokens.prod.json`` (the QBClient's
   FileTokenStore layout — see ``qbsvc.auth.tokens.TokenData``).
2. Sandbox tokens live at ``~/.config/qbsvc/tokens.sandbox.json``. Seed them
   by running the existing OAuth flow with ``QBSVC_INTUIT_ENVIRONMENT=sandbox``
   and a redirected token path — the simplest path is to start the service
   with those env vars set and hit ``/admin/oauth/start`` once.
3. Two dotenv files at the repo root with each realm's Intuit app
   credentials (Intuit issues distinct client_id/secret per app — sandbox
   vs production — and refresh tokens are signed by their app's pair)::

       # .env.production
       QBSVC_INTUIT_CLIENT_ID=...
       QBSVC_INTUIT_CLIENT_SECRET=...

       # .env.sandbox
       QBSVC_INTUIT_CLIENT_ID=...
       QBSVC_INTUIT_CLIENT_SECRET=...

   Both are covered by the repo's ``.env*`` gitignore rule. The script
   reads them directly (not via ``os.environ``) so an exported
   ``QBSVC_INTUIT_*`` from a running service can't leak into either
   client.

Usage
-----
    uv run python scripts/seed_sandbox.py
    uv run python scripts/seed_sandbox.py --dry-run
    uv run python scripts/seed_sandbox.py --only customers
    uv run python scripts/seed_sandbox.py --only items

Behavior
--------
- Copies Active Customers (parents first) and Active Items (parents first,
  Service + Inventory + NonInventory).
- Item account references (Income/Expense/AssetAccountRef) are remapped by
  Account **name** against the sandbox's existing Chart of Accounts. Items
  whose refs can't be resolved are skipped and logged, not created without
  refs.
- Idempotent: ``scripts/.seed_state.json`` records ``{prod_id: sandbox_id}``
  per entity so reruns skip already-copied records.
- Rate limiting is handled by ``QBClient``'s existing TokenBucket.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values

# Make src/ importable when running the file directly with `python scripts/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from qbsvc.api.client import QBClient  # noqa: E402
from qbsvc.auth.tokens import FileTokenStore  # noqa: E402
from qbsvc.config import Settings  # noqa: E402
from qbsvc.exceptions import APIError  # noqa: E402

TOKEN_DIR = Path.home() / ".config" / "qbsvc"
PROD_TOKEN_PATH = TOKEN_DIR / "tokens.prod.json"
SANDBOX_TOKEN_PATH = TOKEN_DIR / "tokens.sandbox.json"
STATE_PATH = _REPO_ROOT / "scripts" / ".seed_state.json"

ENV_FILES = {
    "production": _REPO_ROOT / ".env.production",
    "sandbox": _REPO_ROOT / ".env.sandbox",
}

# Fields that either belong to the source realm (Id, SyncToken) or are
# populated by QBO on create (MetaData). Passing them through causes 400s.
STRIP_FIELDS = ("Id", "SyncToken", "MetaData", "domain", "sparse")

_log = logging.getLogger("seed_sandbox")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def load_state() -> dict[str, dict[str, str]]:
    if not STATE_PATH.exists():
        return {"Customer": {}, "Item": {}}
    data = json.loads(STATE_PATH.read_text())
    data.setdefault("Customer", {})
    data.setdefault("Item", {})
    return data


def save_state(state: dict[str, dict[str, str]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #


def _load_creds(env: str) -> tuple[str, str]:
    """Read QBSVC_INTUIT_CLIENT_ID / _SECRET from the realm's .env file.

    Uses dotenv_values (not load_dotenv) so we never touch os.environ — a
    running service's exported QBSVC_INTUIT_* stays isolated from this
    script's clients.
    """
    path = ENV_FILES[env]
    if not path.exists():
        raise SystemExit(
            f"Missing {path.name}. Create it with QBSVC_INTUIT_CLIENT_ID and "
            "QBSVC_INTUIT_CLIENT_SECRET for this realm's Intuit app "
            f"({'Production' if env == 'production' else 'Development'} Settings "
            "in the Intuit developer dashboard)."
        )
    values = dotenv_values(path)
    client_id = values.get("QBSVC_INTUIT_CLIENT_ID") or ""
    client_secret = values.get("QBSVC_INTUIT_CLIENT_SECRET") or ""
    if not client_id or not client_secret:
        raise SystemExit(
            f"{path.name} is missing QBSVC_INTUIT_CLIENT_ID or "
            "QBSVC_INTUIT_CLIENT_SECRET."
        )
    return client_id, client_secret


def _build_client(env: str, token_path: Path) -> QBClient:
    if not token_path.exists():
        raise SystemExit(
            f"Missing token file for {env}: {token_path}. See the module "
            "docstring for setup steps."
        )
    # Intuit issues distinct client_id/secret pairs per app (sandbox vs
    # production). Refresh tokens expire in ~60min, so a seed run will
    # almost certainly trigger a refresh — each client needs its own creds
    # or the "wrong realm" client 401s.
    client_id, client_secret = _load_creds(env)
    settings = Settings(
        intuit_environment=env,
        intuit_client_id=client_id,
        intuit_client_secret=client_secret,
    )
    return QBClient(FileTokenStore(token_path), settings=settings)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _strip(entity: dict) -> dict:
    clean = copy.deepcopy(entity)
    for f in STRIP_FIELDS:
        clean.pop(f, None)
    return clean


def _paginated_query(client: QBClient, entity: str, where: str = "") -> list[dict]:
    """Page through ``SELECT * FROM <entity>`` with STARTPOSITION/MAXRESULTS.

    QBO caps MAXRESULTS at 1000 and uses 1-based STARTPOSITION.
    """
    results: list[dict] = []
    start = 1
    page = 1000
    where_clause = f" WHERE {where}" if where else ""
    while True:
        sql = (
            f"SELECT * FROM {entity}{where_clause} "
            f"STARTPOSITION {start} MAXRESULTS {page}"
        )
        batch = client.query(sql)
        results.extend(batch)
        if len(batch) < page:
            return results
        start += page


def _topo_sort(entities: list[dict], parent_key: str = "ParentRef") -> list[dict]:
    """Return entities ordered so every parent precedes its children.

    Falls back to insertion order for unrelated records. Detects cycles by
    bailing out of the loop when no progress is made — QBO shouldn't produce
    them, but a bad export shouldn't spin forever.
    """
    by_id = {e["Id"]: e for e in entities}
    ordered: list[dict] = []
    placed: set[str] = set()

    def can_place(e: dict) -> bool:
        parent = e.get(parent_key)
        if not parent:
            return True
        pid = parent.get("value")
        # Parent outside our result set (e.g. inactive parent filtered out)
        # — treat as placeable; we'll just drop the ref later.
        return pid not in by_id or pid in placed

    remaining = list(entities)
    while remaining:
        progress = False
        next_round: list[dict] = []
        for e in remaining:
            if can_place(e):
                ordered.append(e)
                placed.add(e["Id"])
                progress = True
            else:
                next_round.append(e)
        remaining = next_round
        if not progress:
            _log.warning(
                "topo_sort_cycle_or_orphan count=%d — appending as-is",
                len(remaining),
            )
            ordered.extend(remaining)
            break
    return ordered


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #


def seed_customers(
    prod: QBClient,
    sandbox: QBClient,
    state: dict[str, dict[str, str]],
    dry_run: bool,
) -> None:
    customers = _paginated_query(prod, "Customer", "Active = true")
    _log.info("fetched customers count=%d", len(customers))

    ordered = _topo_sort(customers)
    customer_map = state["Customer"]

    for c in ordered:
        prod_id = c["Id"]
        if prod_id in customer_map:
            continue

        payload = _strip(c)
        parent = payload.get("ParentRef")
        if parent:
            sandbox_parent = customer_map.get(parent.get("value"))
            if sandbox_parent:
                parent["value"] = sandbox_parent
            else:
                # Parent didn't make it into sandbox (inactive, error, etc).
                # Drop the ref rather than fail the child; log for review.
                _log.warning(
                    "customer_parent_unresolved prod_id=%s parent=%s — dropping ParentRef",
                    prod_id,
                    parent.get("value"),
                )
                payload.pop("ParentRef", None)
                payload.pop("Job", None)

        if dry_run:
            _log.info("dry_run customer prod_id=%s name=%s", prod_id, c.get("DisplayName"))
            continue

        try:
            resp = sandbox.post("customer", payload)
        except APIError as e:
            _log.error(
                "customer_create_failed prod_id=%s name=%s status=%s detail=%s",
                prod_id,
                c.get("DisplayName"),
                e.status_code,
                e.detail,
            )
            continue

        sandbox_id = resp.get("Customer", {}).get("Id")
        if not sandbox_id:
            _log.error("customer_create_missing_id prod_id=%s resp=%s", prod_id, resp)
            continue

        customer_map[prod_id] = sandbox_id
        save_state(state)
        _log.info("customer_created prod_id=%s sandbox_id=%s", prod_id, sandbox_id)


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #

_ACCOUNT_REF_FIELDS = ("IncomeAccountRef", "ExpenseAccountRef", "AssetAccountRef")


def _sandbox_account_map(sandbox: QBClient) -> dict[str, str]:
    accounts = _paginated_query(sandbox, "Account")
    return {a["Name"]: a["Id"] for a in accounts if a.get("Name") and a.get("Id")}


def _remap_account_refs(
    payload: dict, account_map: dict[str, str]
) -> tuple[bool, list[str]]:
    """Rewrite account ref .value fields by matching .name against sandbox.

    Returns (ok, missing_names). ok=False means at least one required ref
    couldn't be resolved and the item should be skipped.
    """
    missing: list[str] = []
    for field in _ACCOUNT_REF_FIELDS:
        ref = payload.get(field)
        if not ref:
            continue
        name = ref.get("name")
        if not name:
            missing.append(f"{field}(no name)")
            continue
        sandbox_id = account_map.get(name)
        if not sandbox_id:
            missing.append(f"{field}={name}")
            continue
        ref["value"] = sandbox_id
    return (not missing, missing)


def seed_items(
    prod: QBClient,
    sandbox: QBClient,
    state: dict[str, dict[str, str]],
    dry_run: bool,
) -> None:
    account_map = _sandbox_account_map(sandbox)
    _log.info("sandbox accounts loaded count=%d", len(account_map))

    items = _paginated_query(prod, "Item", "Active = true")
    _log.info("fetched items count=%d", len(items))

    ordered = _topo_sort(items)
    item_map = state["Item"]

    for it in ordered:
        prod_id = it["Id"]
        if prod_id in item_map:
            continue

        payload = _strip(it)

        parent = payload.get("ParentRef")
        if parent:
            sandbox_parent = item_map.get(parent.get("value"))
            if sandbox_parent:
                parent["value"] = sandbox_parent
            else:
                _log.warning(
                    "item_parent_unresolved prod_id=%s parent=%s — dropping ParentRef/SubItem",
                    prod_id,
                    parent.get("value"),
                )
                payload.pop("ParentRef", None)
                payload.pop("SubItem", None)

        ok, missing = _remap_account_refs(payload, account_map)
        if not ok:
            _log.warning(
                "item_skipped_missing_accounts prod_id=%s name=%s missing=%s",
                prod_id,
                it.get("Name"),
                missing,
            )
            continue

        if dry_run:
            _log.info(
                "dry_run item prod_id=%s name=%s type=%s",
                prod_id,
                it.get("Name"),
                it.get("Type"),
            )
            continue

        try:
            resp = sandbox.post("item", payload)
        except APIError as e:
            _log.error(
                "item_create_failed prod_id=%s name=%s status=%s detail=%s",
                prod_id,
                it.get("Name"),
                e.status_code,
                e.detail,
            )
            continue

        sandbox_id = resp.get("Item", {}).get("Id")
        if not sandbox_id:
            _log.error("item_create_missing_id prod_id=%s resp=%s", prod_id, resp)
            continue

        item_map[prod_id] = sandbox_id
        save_state(state)
        _log.info("item_created prod_id=%s sandbox_id=%s", prod_id, sandbox_id)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--only",
        choices=("customers", "items"),
        help="Seed only one entity type.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Query prod and log what would be created; don't POST to sandbox.",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)

    prod = _build_client("production", PROD_TOKEN_PATH)
    sandbox = _build_client("sandbox", SANDBOX_TOKEN_PATH)
    try:
        state = load_state()

        if args.only in (None, "customers"):
            seed_customers(prod, sandbox, state, args.dry_run)
        if args.only in (None, "items"):
            seed_items(prod, sandbox, state, args.dry_run)

        _log.info(
            "done customers=%d items=%d",
            len(state["Customer"]),
            len(state["Item"]),
        )
    finally:
        prod.close()
        sandbox.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
