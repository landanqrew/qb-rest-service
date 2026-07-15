"""Target-realm precheck: refuse to replicate into a non-empty sandbox.

The replicator is create-only — it never deletes. Running it twice, or into a
realm that already has real data, would pile duplicates on top of existing rows
and produce a meaningless test dataset. QBO has no bulk delete, so the "wipe"
half of the full-refresh model is the operator's manual **Clear Data and Reset**
in the Intuit Developer Portal. This precheck verifies that reset actually
happened and, if not, aborts with instructions instead of mutating a dirty realm.

Detection is by count, not by a brittle seed-name allow-list. A freshly-reset
QBO sandbox ships with a small, bounded set of sample Customers and Items; a
realm that's already been replicated into has far more. We flag the target as
"not fresh" when either count exceeds `seed_threshold`. The threshold is
deliberately generous and overridable (`--seed-threshold`) so a change in
Intuit's sample-company size doesn't cause false aborts.
"""

from __future__ import annotations

from dataclasses import dataclass

from qbsvc.api.client import QBClient

from .reader import read_all

# QBO's reset sample company seeds well under this many customers/items. Real
# prod realms (the thing we replicate) have hundreds to thousands, so any count
# above the threshold means the sandbox wasn't reset (or was already populated).
DEFAULT_SEED_THRESHOLD = 50

_RESET_INSTRUCTIONS = (
    "The target sandbox realm is not freshly reset "
    "({entity} count = {count}, threshold = {threshold}).\n"
    "\n"
    "The replicator is create-only and will not mutate a non-empty realm.\n"
    "To reset the sandbox:\n"
    "  1. Open the Intuit Developer Portal → your app → Sandbox.\n"
    "  2. On the sandbox company, click 'Clear Data and Reset' (or delete and\n"
    "     recreate the sandbox company).\n"
    "  3. Re-run OAuth bootstrap against the sandbox realm if its realmId\n"
    "     changed, then re-run this replication.\n"
    "\n"
    "If you are certain the target is safe to add to, re-run with --force."
)


class SandboxNotFreshError(RuntimeError):
    """Target realm has more data than a freshly-reset sandbox would."""


@dataclass
class PrecheckResult:
    customer_count: int
    item_count: int
    threshold: int

    @property
    def is_fresh(self) -> bool:
        return (
            self.customer_count <= self.threshold
            and self.item_count <= self.threshold
        )


def check_target_fresh(
    dst_client: QBClient,
    *,
    seed_threshold: int = DEFAULT_SEED_THRESHOLD,
    force: bool = False,
) -> PrecheckResult:
    """Verify the target looks freshly reset. Raise unless it does (or `force`).

    Counts all Customers and Items (active + inactive) in the target — a prior
    replication's rows count even if some were later deactivated.
    """
    customers = read_all(dst_client, "Customer", where="Active IN (true, false)")
    items = read_all(dst_client, "Item", where="Active IN (true, false)")
    result = PrecheckResult(
        customer_count=len(customers),
        item_count=len(items),
        threshold=seed_threshold,
    )

    if result.is_fresh or force:
        return result

    # Report whichever count tripped the check.
    entity, count = (
        ("Customer", result.customer_count)
        if result.customer_count > seed_threshold
        else ("Item", result.item_count)
    )
    raise SandboxNotFreshError(
        _RESET_INSTRUCTIONS.format(
            entity=entity, count=count, threshold=seed_threshold
        )
    )
