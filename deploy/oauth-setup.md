# Intuit OAuth — first-time setup

qb-service hosts its own OAuth handshake at `/admin/oauth/start` and
`/admin/oauth/callback`. No CLI, no laptop browser callback, no manual
Secret Manager edits in the normal case.

> First time deploying to Cloud Run? Walk through
> [`iam-setup.md`](iam-setup.md) first — it stands up the runtime
> service account and the three Secret Manager secrets this doc
> references (`mwl-qb-client-id`, `mwl-qb-client-secret`,
> `mwl-qb-tokens`). Then `deploy/deploy.sh` builds and deploys the
> service, and you come back here for step 1 (register the callback
> URL) and step 3 (run the authorization flow).

## 1. Register the callback URL in the Intuit developer console

Intuit will only redirect to a redirect URI that exactly matches one
registered for the app.

1. Sign in to [developer.intuit.com](https://developer.intuit.com).
2. Open **My Apps → \<your app\> → Production** (or **Development** for
   sandbox testing).
3. Under **Keys & OAuth → Redirect URIs**, add the URL where this service
   will receive the callback:

   ```
   https://<qb-service-domain>/admin/oauth/callback
   ```

   For local dev pointed at the sandbox, also add:

   ```
   http://localhost:8080/admin/oauth/callback
   ```

4. Save. Intuit propagates this within a minute or two.

## 2. Configure the service

Set on the qb-service deployment (Cloud Run env vars, `.env` locally):

| Env var                       | Example                                                 | Notes                                                                |
| ----------------------------- | ------------------------------------------------------- | -------------------------------------------------------------------- |
| `QBSVC_INTUIT_CLIENT_ID`      | `ABxx...`                                               | From Intuit app **Keys & OAuth**.                                    |
| `QBSVC_INTUIT_CLIENT_SECRET`  | `…`                                                     | Treat as secret. In prod, load from Secret Manager.                  |
| `QBSVC_OAUTH_REDIRECT_URI`    | `https://<qb-service-domain>/admin/oauth/callback`      | **Must match** the URL registered in the Intuit console, byte-for-byte. |
| `QBSVC_OAUTH_STATE_TTL_SECONDS` | `600` (default)                                       | How long a CSRF state token stays valid between `/start` and `/callback`. |
| `QBSVC_TOKEN_BACKEND`         | `file` (dev) or `secret_manager` (prod)                 | Where the rotated refresh token is persisted.                        |

## 3. Run the authorization flow

1. From a browser authenticated with an identity that has
   `roles/run.invoker` on `/admin/*` (see scope doc §7), navigate to:

   ```
   https://<qb-service-domain>/admin/oauth/start
   ```

2. The service redirects to Intuit's consent screen. Sign in with the
   QuickBooks admin user for the realm you want to connect and approve
   the requested scopes (`com.intuit.quickbooks.accounting`).

3. Intuit redirects back to `/admin/oauth/callback?code=…&realmId=…&state=…`.
   The service:

   - Validates the `state` token (CSRF protection).
   - Exchanges `code` for `{access_token, refresh_token}`.
   - Persists tokens via the configured `TokenStore` (file or Secret Manager).
   - Returns a small success page.

## 4. Re-auth

Any time the refresh token is invalidated (scope change, manual revoke,
180-day inactivity expiry, switching Intuit apps), repeat step 3. The
new refresh token overwrites the stored one — for the Secret Manager
backend, that means a new secret version is added.

## Troubleshooting

| Symptom                                         | Likely cause                                                                                  |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `400 State mismatch — possible CSRF or expired request.` | The browser used a stale `/start` link (older than `QBSVC_OAUTH_STATE_TTL_SECONDS`), or the service restarted between `/start` and `/callback`. Click the link from `/start` again. |
| `400 Authorization failed: access_denied`       | You clicked **Cancel** on the Intuit consent screen.                                          |
| `502 Token exchange failed: …invalid_grant…`    | The `redirect_uri` configured here doesn't match the one registered in the Intuit console.    |
| `500 OAuth not configured`                      | `QBSVC_INTUIT_CLIENT_ID`, `QBSVC_INTUIT_CLIENT_SECRET`, or `QBSVC_OAUTH_REDIRECT_URI` is missing from the environment. |
| `500 Tokens exchanged but FAILED TO SAVE: …`    | Intuit issued a new refresh token but the configured `TokenStore` rejected the write (e.g. Secret Manager outage / IAM). The new token is lost — re-run `/admin/oauth/start` once the backend is reachable. |

## See also

- [`iam-setup.md`](iam-setup.md) — runtime service account + Secret Manager bindings + Cloud Run invoker IAM.
- [`cloud-run.yaml`](cloud-run.yaml) and [`deploy.sh`](deploy.sh) — deploy artifacts.
- [`docs/qb-service-scope.md`](../docs/qb-service-scope.md) §7 (auth) and §8 (bootstrap/re-auth flow).
