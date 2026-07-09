"""QBO environment-sync replicator (prod → sandbox).

A standalone out-of-band tool — NOT part of the deployed FastAPI service. It
reuses `qbsvc`'s QBO plumbing (QBClient, build_query, token stores) to copy a
production realm's Accounts, Customers, and Items into a freshly-reset sandbox
realm, re-mapping every cross-entity ID reference as it goes.

See docs/plan-env-sync.md for the design rationale.
"""
