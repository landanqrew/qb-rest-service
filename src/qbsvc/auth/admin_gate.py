"""ASGI middleware enforcing an identity allowlist for /admin/* routes.

Cloud Run IAM is service-level, not path-level — both the admin user and the
web app's runtime service account hold `roles/run.invoker` on the same service
and can therefore reach every URL. Issue #13 decided against splitting into
two Cloud Run services (which would still need an app-layer gate to keep
/admin/* off the data service's image anyway). Instead, this middleware
inspects the caller's identity for /admin/* requests and 403s anything not
on `QBSVC_ADMIN_ALLOWLIST`.

The caller's email is read from the Authorization JWT, which Cloud Run has
already validated at the edge. We do not re-verify the signature here:

  - Cloud Run rejects unsigned / expired / wrong-audience tokens before the
    request reaches our container.
  - Re-verifying inside the container means fetching Google's JWKS on every
    cold start and on every key rotation, which adds latency and a failure
    mode the edge already handles.
  - The JWT is therefore trusted *only* as much as Cloud Run's IAM check
    already trusts it. Defense in depth at the application layer is the
    allowlist itself, not a second signature check.

When `admin_allowlist` is empty the gate is OFF. That matches local dev
(no Cloud Run, no JWT) and keeps the existing test suite running without
needing each test to fabricate a bearer token.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from qbsvc.config import Settings, get_settings
from qbsvc.request_context import get_request_id

_log = logging.getLogger("qbsvc.admin_gate")

_ADMIN_PREFIX = "/admin/"


class AdminGateMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        settings_provider: Callable[[], Settings] = get_settings,
    ) -> None:
        self._app = app
        # Indirection so tests can inject a Settings instance without going
        # through the env-var / lru_cache machinery. Production passes the
        # default `get_settings`.
        self._settings_provider = settings_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(_ADMIN_PREFIX):
            await self._app(scope, receive, send)
            return

        settings = self._settings_provider()
        allowlist = _normalize_allowlist(settings.admin_allowlist)
        if not allowlist:
            # Gate intentionally OFF. Local dev and any deployment that hasn't
            # configured the allowlist falls through to whatever IAM provides.
            await self._app(scope, receive, send)
            return

        authz = _header(scope, "authorization")
        email = extract_email_from_bearer(authz)
        if email is None or email not in allowlist:
            _log.warning(
                "admin_gate denied request",
                extra={
                    "path": path,
                    "email": email or "<missing>",
                    "allowlist_size": len(allowlist),
                },
            )
            await _send_forbidden(send)
            return

        await self._app(scope, receive, send)


def extract_email_from_bearer(authorization: str | None) -> str | None:
    """Pull the `email` claim from a `Bearer <jwt>` header.

    Returns the lowercase email if extraction succeeds, else None. Signature
    is **not** verified — see module docstring for why.
    """
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    try:
        payload_b64 = parts[1]
        # base64url with optional missing padding — JWT spec strips '='.
        padding = "=" * (-len(payload_b64) % 4)
        payload_raw = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_raw)
    except (ValueError, json.JSONDecodeError):
        return None

    email = payload.get("email")
    if not isinstance(email, str) or not email:
        return None
    return email.lower()


def _normalize_allowlist(raw: list[str]) -> set[str]:
    """Lowercase + dedupe for case-insensitive matching against the JWT email."""
    return {entry.strip().lower() for entry in raw if entry and entry.strip()}


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("ascii")
    for k, v in scope.get("headers", []):
        if k.lower() == target:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


async def _send_forbidden(send: Send) -> None:
    """403 envelope. Shape matches qbsvc.errors.envelope so callers parse it
    with the same logic as every other error response."""
    request_id = get_request_id()
    body_dict: dict[str, dict[str, str]] = {
        "error": {
            "code": "ADMIN_FORBIDDEN",
            "message": "Caller is not on the admin allowlist.",
        }
    }
    if request_id:
        body_dict["error"]["request_id"] = request_id
    body = json.dumps(body_dict).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
