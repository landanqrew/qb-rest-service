from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from qbsvc.auth import oauth as oauth_mod
from qbsvc.auth.discovery import get_discovery
from qbsvc.auth.oauth_state import OAuthStateStore
from qbsvc.auth.tokens import TokenStore
from qbsvc.config import Settings, get_settings
from qbsvc.deps import (
    get_oauth_state_store,
    get_token_store,
    require_launch_authorization,
)
from qbsvc.exceptions import AuthError, TokenStoreError

router = APIRouter(prefix="/admin/oauth", tags=["admin"])


@router.get("/start", dependencies=[Depends(require_launch_authorization)])
def start(
    settings: Settings = Depends(get_settings),
    state_store: OAuthStateStore = Depends(get_oauth_state_store),
) -> RedirectResponse:
    if (
        not settings.intuit_client_id
        or not settings.intuit_client_secret
        or not settings.oauth_redirect_uri
    ):
        raise HTTPException(
            status_code=500,
            detail=(
                "OAuth not configured: QBSVC_INTUIT_CLIENT_ID, "
                "QBSVC_INTUIT_CLIENT_SECRET, and QBSVC_OAUTH_REDIRECT_URI "
                "must all be set."
            ),
        )
    state = state_store.create()
    url = oauth_mod.build_authorize_url(
        client_id=settings.intuit_client_id,
        state=state,
        redirect_uri=settings.oauth_redirect_uri,
        authorize_endpoint=get_discovery(settings).authorization_endpoint,
    )
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
def callback(
    code: str | None = Query(default=None),
    realmId: str | None = Query(default=None),  # noqa: N803 - Intuit param name
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
    state_store: OAuthStateStore = Depends(get_oauth_state_store),
    token_store: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    # Intuit-originated errors short-circuit before we consume state. The auth
    # code flow never completed, so there's no replay risk, and leaving the
    # state intact lets the operator retry from the same /start link.
    if error:
        return _html(f"Authorization failed: {error}", 400)
    if not state or not state_store.consume(state):
        return _html("State mismatch — possible CSRF or expired request.", 400)
    if not code:
        return _html("Missing authorization code.", 400)
    if not realmId:
        return _html("Missing realmId (company ID).", 400)

    try:
        tokens = oauth_mod.exchange_code(
            settings=settings,
            code=code,
            realm_id=realmId,
            redirect_uri=settings.oauth_redirect_uri,
        )
    except AuthError as e:
        return _html(f"Token exchange failed: {e}", 502)

    try:
        token_store.save(tokens)
    except TokenStoreError as e:
        # Worst-case: Intuit has already rotated the refresh token on its side,
        # but persistence failed. The rotated value is now lost — surface this
        # loudly so the operator knows to re-auth before the next QBO call.
        return _html(
            f"Tokens exchanged but FAILED TO SAVE: {e} — "
            "re-authorize immediately or the next request will fail.",
            500,
        )

    # Browser-safe bootstrap (issue #51): when a return URL is configured, send
    # the user back to the consuming app instead of a standalone success page,
    # so a Connect-QuickBooks button flow lands them where they started. The
    # return URL is operator-set (trusted), so there's no open-redirect risk.
    if settings.admin_return_url:
        return _return_redirect(settings.admin_return_url, realmId)

    return _success_html(realmId)


@router.get("/disconnect", dependencies=[Depends(require_launch_authorization)])
def disconnect_confirm(
    token_store: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    """Operator-facing confirmation page for tearing down the QBO connection.

    This is the target of the Intuit app's *Disconnect URL*. The page is
    informational + a confirm button; the actual revoke happens on POST. The
    whole `/admin/*` surface is IAM- and allowlist-gated, so only the operator
    can reach it — a fully public self-service disconnect would be a DoS vector
    against this single-tenant integration's one shared connection.
    """
    connected = token_store.load() is not None
    return _disconnect_confirm_html(connected)


@router.post("/disconnect", dependencies=[Depends(require_launch_authorization)])
def disconnect(
    settings: Settings = Depends(get_settings),
    token_store: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    """Revoke the QBO connection at Intuit, then clear the persisted token.

    Order matters: revoke first, and only clear local state once Intuit
    confirms. If revoke fails we leave the token in place so the operator can
    retry rather than lose it to a transient error.

    The whole load -> revoke -> clear sequence runs under
    `token_store.refresh_lock` — the same lock `QBClient._refresh_tokens()`
    holds around its load -> refresh -> save path. Without it a concurrent
    QBO-triggered refresh could interleave: revoke might fire on a token
    Intuit already rotated, or worse, a refresh `save()` could land between
    `revoke()` and `clear()`, leaving `clear()` to wipe a live, un-revoked
    token. Holding the lock makes disconnect and refresh mutually exclusive.
    """
    with token_store.refresh_lock:
        tokens = token_store.load()
        if tokens is None:
            # Idempotent: nothing to revoke or clear.
            return _html(
                "No active QuickBooks connection — already disconnected.", 200
            )

        try:
            oauth_mod.revoke(settings, tokens.refresh_token)
        except AuthError as e:
            return _html(
                f"Disconnect failed — token NOT revoked and the connection was "
                f"left intact so you can retry: {e}",
                502,
            )

        try:
            token_store.clear()
        except TokenStoreError as e:
            # Revoke succeeded, so the token is now dead at Intuit, but it's
            # still persisted locally. Surface loudly — the next QBO call would
            # fail on a revoked token until the store is cleared.
            return _html(
                f"Token revoked at Intuit but FAILED TO CLEAR local store: {e} — "
                "the stored token is now dead; clear it before re-connecting.",
                500,
            )

    return _html(
        "QuickBooks disconnected. The stored token was revoked and cleared; "
        "the service now reports not-authenticated until you re-connect at "
        "/admin/oauth/start.",
        200,
    )


def _html(message: str, status_code: int) -> HTMLResponse:
    """Render `message` as plain text inside a minimal HTML page. Always escapes."""
    body = (
        "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
        f"<h2>{html.escape(message)}</h2></body></html>"
    )
    return HTMLResponse(body, status_code=status_code)


def _disconnect_confirm_html(connected: bool) -> HTMLResponse:
    if connected:
        status_line = "A QuickBooks connection is currently active."
        button = (
            "<form method='post' action='/admin/oauth/disconnect'>"
            "<button type='submit'>Disconnect QuickBooks</button></form>"
        )
    else:
        status_line = "No QuickBooks connection is currently active."
        button = "<p>Nothing to disconnect.</p>"
    body = (
        "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
        "<h2>Disconnect QuickBooks</h2>"
        f"<p>{status_line}</p>"
        "<p>Disconnecting revokes the stored token at Intuit and clears it "
        "from this service. QuickBooks calls will report not-authenticated "
        "until an operator re-connects.</p>"
        f"{button}"
        "</body></html>"
    )
    return HTMLResponse(body, status_code=200)


def _return_redirect(return_url: str, realm_id: str) -> RedirectResponse:
    """Redirect the browser back to the consuming app after a successful connect.

    Appends `qb_connected=1` and the connected `realmId` so the app can confirm
    the outcome. Uses 303 so the browser issues a plain GET to the app.
    """
    sep = "&" if "?" in return_url else "?"
    url = f"{return_url}{sep}qb_connected=1&realmId={quote(realm_id, safe='')}"
    return RedirectResponse(url, status_code=303)


def _success_html(realm_id: str) -> HTMLResponse:
    body = (
        "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
        "<h2>QuickBooks authorization successful.</h2>"
        f"<p>Realm <code>{html.escape(realm_id)}</code> connected. "
        "You can close this tab.</p></body></html>"
    )
    return HTMLResponse(body, status_code=200)
