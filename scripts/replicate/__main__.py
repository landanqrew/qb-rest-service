"""CLI entrypoint: `python -m scripts.replicate` (or the Cloud Run Job).

Wires the two realm clients, runs the precheck, orchestrates the replication,
and writes a JSON run-summary. Two credential modes:

  --backend secret_manager  (default, for the in-network Cloud Run Job)
      Reads source/target tokens from two Secret Manager secrets.
  --backend file            (for local dev)
      Reads source/target tokens from two token JSON files.

Exit codes: 0 = success (possibly with skipped records, see summary),
2 = target sandbox not fresh (precheck abort), 1 = unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from qbsvc.logging import configure_logging

from .clients import build_client, file_store, secret_manager_store
from .orchestrator import replicate
from .precheck import DEFAULT_SEED_THRESHOLD, SandboxNotFreshError, check_target_fresh

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NOT_FRESH = 2

_log = logging.getLogger("qbsvc.replicate")


def _build_stores(args):
    """Return (source_store, target_store) for the chosen backend."""
    if args.backend == "secret_manager":
        if not args.gcp_project:
            raise SystemExit("--gcp-project is required with --backend secret_manager")
        return (
            secret_manager_store(args.gcp_project, args.source_secret),
            secret_manager_store(args.gcp_project, args.target_secret),
        )
    # file backend
    return (
        file_store(Path(args.source_tokens)),
        file_store(Path(args.target_tokens)),
    )


def _progress(entity: str, created: int, read_total: int) -> None:
    # One line per record would be noisy for thousands of rows; log every 25th
    # plus the first, so operators see steady movement in the Job logs.
    if created == 1 or created % 25 == 0:
        print(f"  {entity}: {created}/{read_total} created", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Structured JSON logs to stdout — the same formatter the service uses, so
    # Cloud Logging parses each line into queryable fields (entity, prod_id,
    # intuit_tid, qbo fault codes) rather than opaque text.
    configure_logging(level=args.log_level)
    # httpx logs the full request URL per call at INFO; that's one extra line
    # per QBO call and doubles log volume over thousands of rows. The
    # `qbsvc.qbo` `qbo_call` line already carries method/endpoint/status/tid,
    # so silence httpx below WARNING and keep our own structured trace.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    source_store, target_store = _build_stores(args)
    src_client = build_client(source_store, environment="production")
    dst_client = build_client(target_store, environment="sandbox")

    _log.info(
        "replicate_run_start",
        extra={
            "backend": args.backend,
            "source_secret": args.source_secret,
            "target_secret": args.target_secret,
            "seed_threshold": args.seed_threshold,
            "force": args.force,
        },
    )
    try:
        _log.info("replicate_precheck_start")
        result = check_target_fresh(
            dst_client,
            seed_threshold=args.seed_threshold,
            force=args.force,
        )
        _log.info(
            "replicate_precheck_ok",
            extra={
                "target_customers": result.customer_count,
                "target_items": result.item_count,
                "forced": args.force,
            },
        )

        run = replicate(src_client, dst_client, on_progress=_progress)

        summary = run.to_summary()
        _write_summary(summary, args.summary)
        _log.info(
            "replicate_run_done",
            extra={
                "created_count": run.total_created,
                "skipped_count": run.total_skipped,
                "summary_path": args.summary,
            },
        )
        _print_totals(run)
        return EXIT_OK
    except SandboxNotFreshError as exc:
        # Expected, actionable abort — no stack trace, clear instructions.
        _log.error("replicate_precheck_failed", extra={"reason": str(exc)})
        print(f"\nABORT: {exc}", file=sys.stderr, flush=True)
        return EXIT_NOT_FRESH
    except Exception:  # noqa: BLE001 — top-level guard for a headless Job
        # Unexpected failure: log with a full traceback so the Job's logs carry
        # everything needed to diagnose, then exit non-zero.
        _log.exception("replicate_run_failed")
        return EXIT_FAILURE
    finally:
        src_client.close()
        dst_client.close()


def _write_summary(summary: dict, path: str) -> None:
    text = json.dumps(summary, indent=2) + "\n"
    Path(path).write_text(text)
    print(f"\nRun summary written to {path}", flush=True)


def _print_totals(run) -> None:
    print(
        f"Done: {run.total_created} created, {run.total_skipped} skipped.",
        flush=True,
    )
    for tier in run.tiers:
        if tier.skipped:
            print(f"  {tier.entity}: {len(tier.skipped)} skipped", flush=True)


def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="python -m scripts.replicate",
        description="Replicate prod QBO Accounts/Customers/Items into a sandbox.",
    )
    p.add_argument(
        "--backend",
        choices=("secret_manager", "file"),
        default="secret_manager",
    )
    # secret_manager backend
    p.add_argument("--gcp-project", default="")
    p.add_argument("--source-secret", default="mwl-qb-tokens")
    p.add_argument("--target-secret", default="mwl-qb-tokens-sandbox")
    # file backend
    p.add_argument("--source-tokens", default="")
    p.add_argument("--target-tokens", default="")
    # behaviour
    p.add_argument(
        "--seed-threshold",
        type=int,
        default=DEFAULT_SEED_THRESHOLD,
        help="Max Customers/Items a freshly-reset sandbox may have.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the fresh-sandbox precheck and replicate anyway.",
    )
    p.add_argument("--summary", default="replication-summary.json")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Structured-log verbosity. DEBUG includes per-QBO-call lines.",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
