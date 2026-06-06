# Intuit OAuth — first-time setup

qb-service hosts its own OAuth handshake at `/admin/oauth/start` and
`/admin/oauth/callback`. No CLI, no laptop browser callback, no manual
Secret Manager edits in the normal case.

## Deployed service URLs (2026-06-06, project `qrew-tech-1526597818524`)

| Service    | URL                                              | Notes                                        |
| ---------- | ------------------------------------------------ | -------------------------------------------- |
| qb-service | `https://qb-service-5htcalpr7a-uc.a.run.app`     | IAM-gated; sandbox env (`INTUIT_ENV=sandbox`) |
| qb-pages   | `https://qb-pages-5htcalpr7a-uc.a.run.app`       | Public; host/EULA/privacy pages for Intuit   |

OAuth redirect URI registered with Intuit:
`https://qb-service-5htcalpr7a-uc.a.run.app/admin/oauth/callback`

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
| `QBSVC_INTUIT_ENVIRONMENT`    | `production` (default) or `sandbox`                     | `sandbox` routes QBO entity calls to `sandbox-quickbooks.api.intuit.com`. Pair with the app's Development keys and a sandbox realm ID. |
| `QBSVC_OAUTH_REDIRECT_URI`    | `https://<qb-service-domain>/admin/oauth/callback`      | **Must match** the URL registered in the Intuit console, byte-for-byte. |
| `QBSVC_OAUTH_STATE_TTL_SECONDS` | `600` (default)                                       | How long a CSRF state token stays valid between `/start` and `/callback`. |
| `QBSVC_TOKEN_BACKEND`         | `file` (dev) or `secret_manager` (prod)                 | Where the rotated refresh token is persisted.                        |
| `QBSVC_ADMIN_ALLOWLIST`       | `you@example.com,backup-admin@example.com`              | Comma-separated emails allowed to reach `/admin/*`. **Required in prod** (see "Admin gate" below). Empty disables the gate (local dev). |

## 3. Run the authorization flow

> **Reality check (learned during the first bootstrap, 2026-06-06).** A plain
> browser cannot attach a Cloud Run identity token, so with
> `--no-allow-unauthenticated` the IAM edge 403s both `/admin/oauth/start`
> and Intuit's redirect back to `/admin/oauth/callback`. Two extra pieces
> make the flow work:
>
> 1. **Register a localhost redirect URI** (`http://localhost:8080/admin/oauth/callback`)
>    in the Intuit console and set it as `QBSVC_OAUTH_REDIRECT_URI` on the
>    service. Development keys allow plain-HTTP localhost URIs; production
>    keys do not (open question for the production bootstrap).
> 2. **Run a local forwarder that injects your identity token** and browse
>    through `http://localhost:8080`. Note `gcloud run services proxy` does
>    NOT work here — the token it mints fails the admin gate's email check.
>    Forward with the raw token instead, e.g. a small localhost proxy that
>    adds `Authorization: Bearer $(gcloud auth print-identity-token)` to
>    each request and passes 30x redirects through to the browser
>    unfollowed (the browser must follow the Intuit consent redirect).

1. From a browser authenticated with an identity that has
   `roles/run.invoker` on `/admin/*` (see scope doc §7), navigate to:

   ```
   http://localhost:8080/admin/oauth/start   # through the local forwarder
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

## Admin gate (issue #13)

`/admin/oauth/*` is operator-only — the Lab Intake web app's runtime service
account must **not** be able to call it, because doing so could pivot the
service onto a different Intuit realm or invalidate the refresh token.

Cloud Run IAM is service-level, not path-level: both the admin user and the
web app's service account hold `roles/run.invoker` on the deployed service
and can therefore reach every URL. We close the gap with an application-layer
middleware (`qbsvc.auth.admin_gate.AdminGateMiddleware`) that 403s any
`/admin/*` request whose caller email isn't on `QBSVC_ADMIN_ALLOWLIST`.

### How identity is determined

Cloud Run validates the caller's OIDC ID token at the edge (signature,
expiry, audience) before the request reaches the container, then forwards
the JWT unchanged as `Authorization: Bearer …`. The middleware decodes the
JWT payload (no second signature check — see the module docstring for why)
and reads the `email` claim:

- Google user identities expose their email directly.
- Service-account ID tokens carry the SA email as the `email` claim.

The allowlist is matched case-insensitively. Anything not on the list — or
any request missing/with-an-unparseable JWT — gets 403 with the standard
error envelope.

### Configuration

Set `QBSVC_ADMIN_ALLOWLIST` to a comma-separated list of emails:

```
QBSVC_ADMIN_ALLOWLIST=you@example.com,backup-admin@example.com
```

If the env var is unset or empty the gate is **OFF**. That's the right
default for local dev (no Cloud Run, no JWT to inspect) but means
**production deployments must set it**. `deploy/cloud-run.yaml` includes a
placeholder you fill in before applying.

### Decision (rejected option)

The alternative — two Cloud Run services sharing the same image, one for
admin (`qb-admin..run.app`, admin-only IAM) and one for data
(`qb..run.app`, web-app-SA-only IAM) — gives GCP-native IAM isolation, but
doesn't actually close the gap on its own. The web-app SA, having invoker
on the data service, can still reach `/admin/*` on the data service URL
because the same image hosts those routes. Closing that requires either
(a) an env flag on the data service to disable the admin routes — which is
just this middleware in a different shape — or (b) maintaining two
container images, which adds CI complexity.

The in-app middleware keeps one Cloud Run service, one image, one URL, and
puts the admin/data separation in one auditable place. If we ever need
stronger blast-radius isolation (e.g. the admin surface grows beyond OAuth
bootstrap), the two-service split is still available as a follow-up.

## Troubleshooting

| Symptom                                         | Likely cause                                                                                  |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `400 State mismatch — possible CSRF or expired request.` | The browser used a stale `/start` link (older than `QBSVC_OAUTH_STATE_TTL_SECONDS`), or the service restarted between `/start` and `/callback`. Click the link from `/start` again. |
| `400 Authorization failed: access_denied`       | You clicked **Cancel** on the Intuit consent screen.                                          |
| `502 Token exchange failed: …invalid_grant…`    | The `redirect_uri` configured here doesn't match the one registered in the Intuit console.    |
| `500 OAuth not configured`                      | `QBSVC_INTUIT_CLIENT_ID`, `QBSVC_INTUIT_CLIENT_SECRET`, or `QBSVC_OAUTH_REDIRECT_URI` is missing from the environment. |
| `500 Tokens exchanged but FAILED TO SAVE: …`    | Intuit issued a new refresh token but the configured `TokenStore` rejected the write (e.g. Secret Manager outage / IAM). The new token is lost — re-run `/admin/oauth/start` once the backend is reachable. |
| `403 ADMIN_FORBIDDEN` from `/admin/oauth/*`     | Caller's email isn't on `QBSVC_ADMIN_ALLOWLIST` (or the env var is missing in prod). Add the identity to the allowlist and redeploy. Local-dev surprise: setting `QBSVC_ADMIN_ALLOWLIST` without supplying a bearer token will also 403. |

## See also

- [`iam-setup.md`](iam-setup.md) — runtime service account + Secret Manager bindings + Cloud Run invoker IAM.
- [`cloud-run.yaml`](cloud-run.yaml) and [`deploy.sh`](deploy.sh) — deploy artifacts.
- [`docs/qb-service-scope.md`](../docs/qb-service-scope.md) §7 (auth) and §8 (bootstrap/re-auth flow).
