"""Drives the three-tier replication in dependency order and tallies the run.

Order is load-bearing (a child can't reference a parent Id that doesn't exist
yet in the target):

    Accounts  →  Customers  →  Items

and *within* each hierarchical type, parents before children (topo sort on
ParentRef). Accounts must precede Items because every Item references at least
an IncomeAccountRef.

The orchestrator is transport-agnostic — it takes ready-made source/target
QBClients — so it runs identically under a mock transport in tests and against
live QBO in the Cloud Run Job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from qbsvc.api.client import QBClient
from qbsvc.exceptions import APIError

from .faults import fault_details, one_line
from .mapping import IdMap, RemapError, topo_sort_by_parent
from .reader import read_active
from .writer import Writer

_log = logging.getLogger("qbsvc.replicate")

# Source read order == create order. Accounts first (Items depend on them),
# then Customers, then Items.
TIERS = ["Account", "Customer", "Item"]


@dataclass
class TierResult:
    entity: str
    read: int = 0
    created: int = 0
    skipped: list[dict] = field(default_factory=list)  # {"id","name","reason"}

    def to_summary(self) -> dict:
        return {
            "entity": self.entity,
            "read": self.read,
            "created": self.created,
            "skipped": self.skipped,
        }


@dataclass
class RunResult:
    tiers: list[TierResult] = field(default_factory=list)
    id_maps: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def total_created(self) -> int:
        return sum(t.created for t in self.tiers)

    @property
    def total_skipped(self) -> int:
        return sum(len(t.skipped) for t in self.tiers)

    def to_summary(self) -> dict:
        return {
            "tiers": [t.to_summary() for t in self.tiers],
            "totals": {
                "created": self.total_created,
                "skipped": self.total_skipped,
            },
            "id_maps": self.id_maps,
        }


def _display_name(entity: str, row: dict) -> str:
    if entity == "Customer":
        return row.get("DisplayName", "")
    return row.get("Name", "")


def replicate(
    src_client: QBClient,
    dst_client: QBClient,
    *,
    on_progress=None,
) -> RunResult:
    """Copy Accounts → Customers → Items from source realm to target realm.

    `on_progress(entity, created, read_total)` is an optional callback for
    live logging. Returns a RunResult with per-tier counts, skipped records
    (with reasons), and the full prod→sandbox id-maps for the summary file.
    """
    id_map = IdMap()
    writer = Writer(dst_client, id_map)
    run = RunResult()

    for entity in TIERS:
        tier = TierResult(entity=entity)
        rows = read_active(src_client, entity)
        tier.read = len(rows)
        _log.info(
            "replicate_tier_start",
            extra={"entity": entity, "source_rows": tier.read},
        )

        # Parents before children so ParentRef always resolves.
        ordered = topo_sort_by_parent(rows)

        for row in ordered:
            prod_id = str(row.get("Id", ""))
            name = _display_name(entity, row)
            try:
                sandbox_id = writer.replicate(entity, row)
                tier.created += 1
                _log.info(
                    "replicate_created",
                    extra={
                        "entity": entity,
                        "prod_id": prod_id,
                        "sandbox_id": sandbox_id,
                        # `name` is a reserved LogRecord attribute — use a
                        # distinct key so the record isn't rejected.
                        "display_name": name,
                    },
                )
                if on_progress is not None:
                    on_progress(entity, tier.created, tier.read)
            except RemapError as exc:
                # A dependency wasn't replicated (e.g. an Item's account was
                # inactive and filtered out). Record and keep going — best-
                # effort dataset over aborting the whole run.
                reason = f"unresolved reference: {exc}"
                tier.skipped.append(
                    {"id": prod_id, "name": name, "reason": reason}
                )
                _log.warning(
                    "replicate_skipped",
                    extra={
                        "entity": entity,
                        "prod_id": prod_id,
                        "display_name": name,
                        "skip_kind": "unresolved_reference",
                        "reason": reason,
                    },
                )
            except APIError as exc:
                # QBO rejected this record. Capture the fault code(s) and
                # intuit_tid — the only things that make a headless failure
                # debuggable after the fact — into both the summary and the log.
                details = fault_details(exc)
                tier.skipped.append(
                    {
                        "id": prod_id,
                        "name": name,
                        "reason": one_line(exc),
                        "qbo": details,
                    }
                )
                _log.warning(
                    "replicate_skipped",
                    extra={
                        "entity": entity,
                        "prod_id": prod_id,
                        "display_name": name,
                        "skip_kind": "qbo_error",
                        **details,
                    },
                )

        _log.info(
            "replicate_tier_done",
            extra={
                "entity": entity,
                # `created` is a reserved LogRecord attribute (the record
                # timestamp); use explicit *_count keys to avoid the clash.
                "created_count": tier.created,
                "skipped_count": len(tier.skipped),
            },
        )
        run.tiers.append(tier)
        run.id_maps[entity] = id_map.get(entity)

    return run
