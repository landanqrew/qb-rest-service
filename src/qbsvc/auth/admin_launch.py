"""Browser-safe OAuth *launch token* (issue #51).

The deployed data service is IAM-locked at the Cloud Run edge, so a real
browser — which cannot attach a Cloud Run identity token — is 403'd before it
reaches FastAPI. That makes a user-facing "Connect QuickBooks" button
impossible against the data service. Issue #51 adds a browser-safe OAuth
bootstrap surface (see ``deploy/qb-admin-setup.md``); this module is the
capability that keeps that surface from being open to the whole internet.

A **launch token** is a short-lived, HMAC-signed string minted by the consuming
app (e.g. consuming app), which already authenticates its own admins. The
consuming app shares ``QBSVC_ADMIN_LAUNCH_SECRET`` with the bootstrap service
and reproduces ``mint_launch_token`` server-side, then renders the button as a
link to ``/admin/oauth/start?launch=<token>``. The bootstrap service verifies
the token before starting the Intuit flow. A visitor without the shared secret
cannot forge one, so only an admin-initiated click gets through.

The token intentionally carries **no identity and no return URL** — only an
expiry and a signature over it. It is a bearer capability ("this click was
authorized by the consuming app, recently"), not a session. The one-time-ness
of the actual OAuth handshake is enforced downstream by the CSRF ``state`` token
(:mod:`qbsvc.auth.oauth_state`); the launch token only guards *initiation*.
Keeping it minimal also keeps it dependency-free (stdlib ``hmac`` only) and
trivial for the consuming app to re-implement in any language.
"""
from __future__ import annotations

import hashlib
import hmac
import time

# Domain-separation label folded into the HMAC key so the same shared secret
# used for anything else can never collide with a launch-token signature.
_KEY_CONTEXT = b"qbsvc-admin-launch-v1"


def _signing_key(secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()


def _sign(secret: str, exp: str) -> str:
    return hmac.new(_signing_key(secret), exp.encode("ascii"), hashlib.sha256).hexdigest()


def mint_launch_token(secret: str, ttl_seconds: int, now: float | None = None) -> str:
    """Mint a launch token that expires ``ttl_seconds`` from ``now``.

    Format: ``"<exp>.<hex-signature>"`` where ``exp`` is an integer unix
    timestamp. The consuming app calls this (or an equivalent in its own
    language) with the shared secret to authorize a Connect-QuickBooks click.
    """
    issued = time.time() if now is None else now
    exp = str(int(issued) + int(ttl_seconds))
    return f"{exp}.{_sign(secret, exp)}"


def verify_launch_token(token: str | None, secret: str, now: float | None = None) -> bool:
    """Return True iff ``token`` is well-formed, correctly signed, and unexpired.

    Fails closed on every error path: missing/empty token, empty secret
    (misconfiguration), malformed structure, bad signature, or expiry. The
    signature check is constant-time.
    """
    if not token or not secret:
        return False

    exp_str, dot, sig = token.partition(".")
    if not dot or not exp_str or not sig:
        return False

    try:
        exp = int(exp_str)
    except ValueError:
        return False

    if not hmac.compare_digest(sig, _sign(secret, exp_str)):
        return False

    current = time.time() if now is None else now
    return current < exp
