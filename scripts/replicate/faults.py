"""Extract structured diagnostics from a QBO APIError.

QBO packs the real reason for a rejection into `Fault.Error[]` (a code, a
human message, and the offending element name) and returns a per-request
transaction id (`intuit_tid`) that Intuit support keys off. The route layer
surfaces these to HTTP callers; the replicator runs headless, so it must pull
the same detail into its logs and run-summary or a failed record is
undebuggable after the fact.
"""

from __future__ import annotations

from qbsvc.exceptions import APIError


def fault_details(exc: APIError) -> dict:
    """Flatten an APIError into a JSON-friendly diagnostic dict.

    Always includes `status` and `detail`; adds `intuit_tid` when present and
    the parsed QBO `Fault.Error[]` list (code / message / element) when the
    raw body carried one. Everything here is safe to log and to embed in the
    run-summary's skipped-record entries.
    """
    info: dict = {
        "status": exc.status_code,
        "detail": exc.detail,
    }
    if exc.intuit_tid:
        info["intuit_tid"] = exc.intuit_tid

    raw = exc.raw
    if isinstance(raw, dict):
        fault = raw.get("Fault") or {}
        errors = []
        for err in fault.get("Error", []):
            if not isinstance(err, dict):
                continue
            errors.append(
                {
                    "code": str(err.get("code", "")),
                    "message": err.get("Message", ""),
                    "detail": err.get("Detail", ""),
                    "element": err.get("element", ""),
                }
            )
        if errors:
            info["qbo_errors"] = errors
        if fault.get("type"):
            info["fault_type"] = fault["type"]
    return info


def one_line(exc: APIError) -> str:
    """A compact human string for the run-summary `reason` field.

    Leads with the QBO fault code(s) when available — that's the signal that
    identifies the failure mode (6240 duplicate, 5010 stale token, …) — then
    the message and the intuit_tid so a single line is enough to act on.
    """
    details = fault_details(exc)
    parts = [f"QBO {exc.status_code}"]
    for err in details.get("qbo_errors", []):
        code = err["code"]
        msg = err["message"] or err["detail"]
        parts.append(f"[{code}] {msg}" if code else msg)
    if not details.get("qbo_errors"):
        parts.append(exc.detail)
    if details.get("intuit_tid"):
        parts.append(f"(intuit_tid={details['intuit_tid']})")
    return " ".join(p for p in parts if p)
