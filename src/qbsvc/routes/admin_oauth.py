from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from qbsvc.auth import oauth as oauth_mod
from qbsvc.auth.oauth_state import OAuthStateStore
from qbsvc.auth.tokens import TokenStore
from qbsvc.config import Settings, get_settings
from qbsvc.deps import get_oauth_state_store, get_token_store
from qbsvc.exceptions import AuthError

router = APIRouter(prefix="/admin/oauth", tags=["admin"])


@router.get("/start")
def start(
    settings: Settings = Depends(get_settings),
    state_store: OAuthStateStore = Depends(get_oauth_state_store),
) -> RedirectResponse:
    if not settings.intuit_client_id or not settings.oauth_redirect_uri:
        raise HTTPException(
            status_code=500,
            detail=(
                "OAuth not configured: QBSVC_INTUIT_CLIENT_ID and "
                "QBSVC_OAUTH_REDIRECT_URI must be set."
            ),
        )
    state = state_store.create()
    url = oauth_mod.build_authorize_url(
        client_id=settings.intuit_client_id,
        state=state,
        redirect_uri=settings.oauth_redirect_uri,
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
    if not state or not state_store.consume(state):
        return _html("State mismatch — possible CSRF or expired request.", 400)
    if error:
        return _html(f"Authorization failed: {html.escape(error)}", 400)
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
        return _html(f"Token exchange failed: {html.escape(str(e))}", 502)

    token_store.save(tokens)
    return _html(
        "QuickBooks authorization successful. "
        f"Realm <code>{html.escape(realmId)}</code> connected. "
        "You can close this tab.",
        200,
    )


def _html(message: str, status_code: int) -> HTMLResponse:
    body = (
        "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
        f"<h2>{message}</h2></body></html>"
    )
    return HTMLResponse(body, status_code=status_code)
