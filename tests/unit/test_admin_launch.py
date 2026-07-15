"""Unit tests for the browser-safe OAuth *launch token* (issue #51).

A launch token is a short-lived, HMAC-signed capability minted by the consuming
app (e.g. consuming app, which already gates its own admins) and carried on the
`/admin/oauth/start` link. It lets a real browser start the Intuit OAuth flow
without a Cloud Run identity token, while keeping random public visitors out:
they cannot forge a token without the shared secret.

The token is deliberately tiny and dependency-free (stdlib hmac only): an
expiry plus a signature over that expiry. Anyone holding the shared secret can
reproduce `mint_launch_token`, so the consuming app mints its own.
"""
from __future__ import annotations

from qbsvc.auth import admin_launch

SECRET = "shared-launch-secret-value"


def test_minted_token_validates() -> None:
    token = admin_launch.mint_launch_token(SECRET, ttl_seconds=300, now=1000.0)
    assert admin_launch.verify_launch_token(token, SECRET, now=1000.0) is True


def test_token_valid_until_expiry_then_rejected() -> None:
    token = admin_launch.mint_launch_token(SECRET, ttl_seconds=300, now=1000.0)
    # Just before expiry: still valid.
    assert admin_launch.verify_launch_token(token, SECRET, now=1299.0) is True
    # At/after expiry: rejected.
    assert admin_launch.verify_launch_token(token, SECRET, now=1300.0) is False
    assert admin_launch.verify_launch_token(token, SECRET, now=5000.0) is False


def test_wrong_secret_is_rejected() -> None:
    token = admin_launch.mint_launch_token(SECRET, ttl_seconds=300, now=1000.0)
    assert admin_launch.verify_launch_token(token, "other-secret", now=1000.0) is False


def test_tampered_signature_is_rejected() -> None:
    token = admin_launch.mint_launch_token(SECRET, ttl_seconds=300, now=1000.0)
    exp, _, _sig = token.partition(".")
    forged = f"{exp}.{'0' * len(_sig)}"
    assert admin_launch.verify_launch_token(forged, SECRET, now=1000.0) is False


def test_tampered_expiry_is_rejected() -> None:
    """Bumping the expiry without re-signing must fail — the signature covers it."""
    token = admin_launch.mint_launch_token(SECRET, ttl_seconds=1, now=1000.0)
    _exp, _, sig = token.partition(".")
    forged = f"9999999999.{sig}"
    assert admin_launch.verify_launch_token(forged, SECRET, now=1000.0) is False


def test_malformed_tokens_are_rejected() -> None:
    for bad in ("", "no-dot", "abc.def", ".", "123.", "12.34.56"):
        assert admin_launch.verify_launch_token(bad, SECRET, now=1000.0) is False


def test_none_token_is_rejected() -> None:
    assert admin_launch.verify_launch_token(None, SECRET, now=1000.0) is False


def test_empty_secret_never_validates() -> None:
    """A misconfiguration (no secret) must never accept a token, even a
    structurally valid one — fail closed."""
    token = admin_launch.mint_launch_token(SECRET, ttl_seconds=300, now=1000.0)
    assert admin_launch.verify_launch_token(token, "", now=1000.0) is False
