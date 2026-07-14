"""Seed the QBO sandbox realm with Customers and Items copied from production.

One-off developer script. Not part of the runtime service.

Prerequisites
-------------
1. Prod Customer + Item data exported to JSON at
   ``~/qb-exports/prod/{customers,items}.json`` (full QBO entities). The
   script reads prod from these files rather than querying prod live, so it
   never holds a prod refresh token — no chance of rotating the token out
   from under the deployed prod service.
2. Sandbox tokens live at ``~/.config/qbsvc/tokens.sandbox.json`` (the
   QBClient's FileTokenStore layout — see ``qbsvc.auth.tokens.TokenData``).
   Populate them by connecting the sandbox realm through qb-admin-sandbox,
   then exporting the ``qbsvc-sandbox-token-store`` blob to that path.
3. A dotenv file at the repo root with the SANDBOX Intuit app credentials
   (only the sandbox client is built now that prod comes from files)::

       # .env.sandbox
       QBSVC_INTUIT_CLIENT_ID=...
       QBSVC_INTUIT_CLIENT_SECRET=...

   Covered by the repo's ``.env*`` gitignore rule. Read via ``dotenv_values``
   (not ``os.environ``) so a running service's exported ``QBSVC_INTUIT_*``
   can't leak into the client.

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
import time
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values

# Make src/ importable when running the file directly with `python scripts/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from qbsvc.api.client import QBClient  # noqa: E402
from qbsvc.auth.tokens import FileTokenStore  # noqa: E402
from qbsvc.config import Settings  # noqa: E402
from qbsvc.exceptions import APIError, RateLimitError  # noqa: E402

TOKEN_DIR = Path.home() / ".config" / "qbsvc"
SANDBOX_TOKEN_PATH = TOKEN_DIR / "tokens.sandbox.json"
EXPORT_DIR = Path.home() / "qb-exports" / "prod"
STATE_PATH = _REPO_ROOT / "scripts" / ".seed_state.json"

ENV_FILES = {
    "sandbox": _REPO_ROOT / ".env.sandbox",
}

# Fields that either belong to the source realm (Id, SyncToken) or are
# populated by QBO on create (MetaData). Passing them through causes 400s.
STRIP_FIELDS = ("Id", "SyncToken", "MetaData", "domain", "sparse")

# Refs on prod Customers pointing at prod-realm object ids with no sandbox
# equivalent by id (sandbox has its own Terms / PaymentMethods / CustomerTypes).
# Dropped for seeding — test data doesn't need these presets, and passing a
# prod id yields "Invalid Reference Id ... not found". CurrencyRef is kept: its
# value is a currency code ("USD"), not a realm id.
CUSTOMER_STRIP_REFS = ("CustomerTypeRef", "PaymentMethodRef", "SalesTermRef")

# The sandbox rejects sustained write bursts with a generic 500-style error
# wrapped in a 400 ("unexpected error ... please wait and try again"). Pace
# writes and back off on that (and on the local bucket's RateLimitError).
SANDBOX_WRITE_DELAY_SEC = 0.5
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

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


def _load_prod_entities(name: str) -> list[dict]:
    """Load Active prod records from the JSON export (not a live prod query).

    ``name`` is the export basename (``customers`` / ``items``). Filters to
    Active records to match the original ``SELECT ... WHERE Active = true``
    behavior; the export itself includes inactive rows.
    """
    path = EXPORT_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(
            f"Missing prod export {path}. Produce it first with the read-only "
            "export against the deployed data API."
        )
    records = json.loads(path.read_text())
    return [r for r in records if r.get("Active") is True]


def _strip(entity: dict) -> dict:
    clean = copy.deepcopy(entity)
    for f in STRIP_FIELDS:
        clean.pop(f, None)
    return clean


def _post_with_retry(
    client: QBClient, endpoint: str, payload: dict, *, attempts: int = 5
) -> dict:
    """POST with exponential backoff on transient sandbox/QBO errors.

    The sandbox returns a generic "unexpected error ... please wait and try
    again" under write load, and the local token bucket raises RateLimitError
    if we outrun it — both retryable. Deterministic validation errors (bad
    ref, duplicate name) re-raise immediately so the caller logs and skips.
    """
    for i in range(attempts):
        try:
            return client.post(endpoint, payload)
        except (APIError, RateLimitError) as e:
            if isinstance(e, APIError):
                detail = (e.detail or "").lower()
                transient = (
                    e.status_code in _TRANSIENT_STATUS
                    or "unexpected error" in detail
                    or "please wait" in detail
                )
            else:  # RateLimitError — always retryable
                transient = True
            if not transient or i == attempts - 1:
                raise
            delay = 2.0 * (2**i)  # 2, 4, 8, 16s
            _log.warning(
                "transient_post_retry endpoint=%s attempt=%d delay=%.0fs",
                endpoint,
                i + 1,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # last attempt always returns or raises


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
    sandbox: QBClient,
    customers: list[dict],
    state: dict[str, dict[str, str]],
    dry_run: bool,
    limit: int | None = None,
) -> None:
    _log.info("prod customers loaded count=%d", len(customers))

    ordered = _topo_sort(customers)
    if limit is not None:
        ordered = ordered[:limit]
        _log.info("customer limit applied count=%d", len(ordered))
    customer_map = state["Customer"]

    for c in ordered:
        prod_id = c["Id"]
        if prod_id in customer_map:
            continue

        payload = _strip(c)
        for ref in CUSTOMER_STRIP_REFS:
            payload.pop(ref, None)
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

        time.sleep(SANDBOX_WRITE_DELAY_SEC)  # pace writes so the sandbox doesn't choke
        try:
            resp = _post_with_retry(sandbox, "customer", payload)
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
    sandbox: QBClient,
    items: list[dict],
    state: dict[str, dict[str, str]],
    dry_run: bool,
    limit: int | None = None,
) -> None:
    account_map = _sandbox_account_map(sandbox)
    _log.info("sandbox accounts loaded count=%d", len(account_map))

    _log.info("prod items loaded count=%d", len(items))

    ordered = _topo_sort(items)
    if limit is not None:
        ordered = ordered[:limit]
        _log.info("item limit applied count=%d", len(ordered))
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

        time.sleep(SANDBOX_WRITE_DELAY_SEC)  # pace writes so the sandbox doesn't choke
        try:
            resp = _post_with_retry(sandbox, "item", payload)
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
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap creates per entity (after topo-sort). Use for a small test batch.",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)

    sandbox = _build_client("sandbox", SANDBOX_TOKEN_PATH)
    try:
        state = load_state()

        if args.only in (None, "customers"):
            seed_customers(
                sandbox, _load_prod_entities("customers"), state, args.dry_run, args.limit
            )
        if args.only in (None, "items"):
            seed_items(
                sandbox, _load_prod_entities("items"), state, args.dry_run, args.limit
            )

        _log.info(
            "done customers=%d items=%d",
            len(state["Customer"]),
            len(state["Item"]),
        )
    finally:
        sandbox.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
