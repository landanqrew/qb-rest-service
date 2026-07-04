# Intuit OAuth — first-time setup

The OAuth handshake (`/admin/oauth/start` + `/admin/oauth/callback`) runs on
the **qb-admin** service — a public, browser-safe companion that shares
qb-service's image and its `mwl-qb-tokens` secret (issue #52). The deployed
**qb-service** is IAM-locked and data-only (`QBSVC_ENABLE_ADMIN_ROUTES=false`),
so it no longer hosts those routes. See
[`qb-admin-setup.md`](qb-admin-setup.md) for the production browser flow; this
doc covers registering the Intuit callback and the local-forwarder bootstrap
(§3a) that also writes the shared token secret. No CLI, no laptop browser
callback, no manual Secret Manager edits in the normal case.

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
>    service. Development keys allow plain-HTTP localhost URIs; **production
>    keys reject localhost and IP redirect URIs entirely** (they must be a
>    publicly resolvable `https://` host). The production bootstrap therefore
>    uses a different mechanism — see
>    [§3a Production bootstrap](#3a-production-bootstrap-production-keys--locked-edge).
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

> The steps above describe the **dev / sandbox** flow (Development keys,
> `http://localhost` redirect URI, identity-injecting forwarder). For
> **production keys** against the IAM-locked deployed service, use §3a.

## 3a. Production bootstrap (production keys + locked edge)

Production keys reject localhost/IP redirect URIs, so the callback must be a
public `https://` URL. But the deployed `qb-service` is
`--no-allow-unauthenticated`: a browser following Intuit's redirect carries no
Cloud Run identity token, so the IAM edge 403s the callback. Rather than open
the production edge, run the **one-time** handshake from a throwaway public
tunnel pointed at a *local* instance, and let it write the token to the **same
Secret Manager secret** the deployed service reads. The deployed service's
locked edge is never touched; it picks up the token on its next call.

```
local uvicorn  --(temporary https tunnel)-->  Intuit consent
      |
      writes token version --> Secret Manager (mwl-qb-tokens)
      |
deployed qb-service reads it on the next QBO call
```

This is a one-time operation: once a refresh token lands in Secret Manager it
self-sustains via rotation (§4). You only repeat it on revoke / scope change /
180-day inactivity.

### Prerequisites

- **Production** Intuit app keys (client id + secret) from the Intuit console.
- The `mwl-qb-tokens` secret already **exists** in the target project (it can
  be empty). See [`iam-setup.md`](iam-setup.md).
- Your Google identity holds **`roles/secretmanager.secretVersionAdder`** (and
  `secretAccessor`) on `mwl-qb-tokens`, then:
  ```bash
  gcloud auth application-default login
  ```
- A temporary public HTTPS tunnel tool (e.g. `ngrok`), authenticated.
- **Move `.env` aside** for the duration so its sandbox credentials cannot
  bleed into the prod run (`mv .env .env.sandbox` — restore after). Shell env
  vars do override `.env`, but removing the ambiguity is safer.

### Procedure

1. **Open the tunnel** to the local port you'll run on:
   ```bash
   ngrok http 8080      # note the https URL, e.g. https://ab12cd34.ngrok-free.app
   ```
   If your tunnel plan supports IP restriction, allow only your own public IP.
   The entire flow — `/start` and the callback — originates from *your*
   browser, so an IP allowlist won't break it. Do **not** put basic-auth on the
   tunnel: Intuit's browser redirect to `/callback` can't supply credentials.

2. **Register the tunnel callback** as a **Production** redirect URI in the
   Intuit console (Keys & OAuth → Redirect URIs):
   ```
   https://<tunnel-subdomain>.ngrok-free.app/admin/oauth/callback
   ```
   Save and wait ~1–2 minutes for Intuit to propagate it.

3. **Start a local instance** wired to production keys and the shared secret.
   The redirect URI must match step 2 byte-for-byte:
   ```bash
   QBSVC_INTUIT_CLIENT_ID='<prod-client-id>' \
   QBSVC_INTUIT_CLIENT_SECRET='<prod-client-secret>' \
   QBSVC_INTUIT_ENVIRONMENT=production \
   QBSVC_TOKEN_BACKEND=secret_manager \
   QBSVC_GCP_PROJECT='<target-project>' \
   QBSVC_SECRET_NAME_TOKENS=mwl-qb-tokens \
   QBSVC_ADMIN_ALLOWLIST= \
   QBSVC_OAUTH_REDIRECT_URI='https://<tunnel-subdomain>.ngrok-free.app/admin/oauth/callback' \
   uv run uvicorn qbsvc.main:app --port 8080
   ```
   `QBSVC_ADMIN_ALLOWLIST=` (empty) keeps the admin gate **off** — required,
   because Intuit's browser callback carries no JWT for the gate to check. The
   gate being off is why the tunnel must stay short-lived / IP-restricted.

4. In your browser, go to the **tunnel** start URL (not localhost):
   ```
   https://<tunnel-subdomain>.ngrok-free.app/admin/oauth/start
   ```

5. Approve consent as the QuickBooks admin **for the production realm**. Intuit
   redirects your browser → tunnel → local `/admin/oauth/callback`, which
   exchanges the code and writes a **new version** of `mwl-qb-tokens`.

6. **Tear down immediately:** stop uvicorn, stop the tunnel, and **delete the
   tunnel redirect URI** from the Intuit console (it's now dead). Restore your
   env file: `mv .env.sandbox .env`.

7. **Verify** against the deployed, still-locked service with an authenticated
   call (identity that has `roles/run.invoker`):
   ```bash
   curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     https://<qb-service-domain>/v1/customers
   ```
   A 200 with customer data confirms the deployed service is reading the new
   token from Secret Manager.

> **Why this is safe:** the production edge stays `--no-allow-unauthenticated`
> throughout. The only exposed surface is a random, short-lived tunnel URL to a
> local process, and the token it produces is shared via Secret Manager — so it
> makes no difference that the handshake didn't run on the deployed service.

## 3b. Browser-safe production bootstrap via `qb-admin` (issue #51)

The tunnel bootstrap (§3a) is an operator fallback. For a real, user-facing
**Connect QuickBooks** button in a consuming app, deploy the **`qb-admin`**
companion — the same image as a *public* Cloud Run service that hosts only
`/admin/oauth/*` (data routes off), gated by a signed **launch token** the
consuming app mints (not Cloud Run IAM, which a browser can't satisfy). Both
services share the `mwl-qb-tokens` secret, so a connect/re-auth done through
`qb-admin` is immediately live for the IAM-locked `qb-service`.

This replaces the throwaway tunnel for production: register
`{qb-admin-url}/admin/oauth/callback` as the Intuit redirect URI (a public
`https://` URL, which production keys require) and point the app's button at
`{qb-admin-url}/admin/oauth/start?launch=<token>`. Full setup, the launch-token
scheme, and the button integration are in
[`qb-admin-setup.md`](qb-admin-setup.md).

## 4. Re-auth

Any time the refresh token is invalidated (scope change, manual revoke,
180-day inactivity expiry, switching Intuit apps), repeat the authorization
flow — §3 for dev/sandbox, **§3a for production**. The new refresh token
overwrites the stored one — for the Secret Manager backend, that means a new
secret version is added.

## 4b. Disconnect / revoke (issue #49)

The Intuit app-assessment questionnaire requires a **Disconnect URL** — where a
customer goes to tear down their connection. qb-service exposes this as an
**admin-only** route:

```
GET  /admin/oauth/disconnect   # confirmation page (is a connection active?)
POST /admin/oauth/disconnect   # revoke at Intuit, then clear the stored token
```

The `POST` handler:

1. Loads the stored token. If there is none, it's a no-op (already
   disconnected).
2. **Revokes** the refresh token at the discovery document's
   `revocation_endpoint` (HTTP Basic auth with the client credentials,
   `{"token": …}` body). Revoking the refresh token invalidates the whole grant.
3. On a successful revoke, **clears** the `TokenStore`. For the Secret Manager
   backend this appends a tombstone version (an empty JSON object) rather than
   deleting history, so `load()` reads as not-authenticated while prior token
   versions remain for audit. For the file backend it deletes the file.

After a successful disconnect, subsequent QBO calls return `NOT_AUTHENTICATED`
until the operator re-connects via §3 / §3a.

**Failure handling.** If the revoke call returns non-200, the route returns
`502` and **leaves the stored token in place** — a transient revoke error must
not cost the operator their connection; retry the disconnect. If the revoke
succeeds but the store clear fails, the route returns `500` with a loud message
(the token is now dead at Intuit but still persisted locally — clear it before
re-connecting).

### Reachability decision (admin-only)

`/admin/*` is IAM-locked (`--no-allow-unauthenticated`) **and**
email-allowlisted (`AdminGateMiddleware`), so the disconnect route has the same
edge posture as `/admin/oauth/start` (see §3a): an unauthenticated browser gets
`403`. That is deliberate. Because this is a **single-tenant** integration with
one shared realm connection, a fully public self-service disconnect would be an
abuse/DoS vector — anyone could revoke the operator's only connection. We
therefore keep disconnect operator-gated rather than adding a public
self-service page on qb-pages (whose surface stays frozen at three static
pages).

**Intuit Disconnect URL to register:** set the questionnaire's Disconnect URL to

```
https://<qb-service-domain>/admin/oauth/disconnect
```

An Intuit reviewer's browser will see the standard `403` (no Cloud Run identity
token) — the same constraint that applies to the connect/reconnect URL. The
operator reaches it authenticated through the identity-injecting forwarder used
for the connect flow (§3).

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

This is enforced two ways so a misconfigured deploy can't silently disable the
gate:

- `deploy/deploy.sh` hard-requires `ADMIN_ALLOWLIST` (it errors before building).
- The app itself **refuses to start on Cloud Run** with an empty allowlist:
  `ensure_admin_gate_configured()` (called from `create_app`) raises when
  `K_SERVICE` is set (i.e. running on Cloud Run) but the allowlist is empty, so
  a deploy via raw `gcloud run deploy` / `services replace` fails its startup
  probe instead of coming up wide open. Local dev and the §3a bootstrap have no
  `K_SERVICE`, so they're unaffected and the gate stays off there as intended.

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
| `502 Disconnect failed — token NOT revoked …`   | Intuit's revoke endpoint returned non-200 (network, already-invalid token, or bad creds). The stored token was left intact — retry `/admin/oauth/disconnect`. |
| `500 Token revoked at Intuit but FAILED TO CLEAR …` | Revoke succeeded but the `TokenStore` write failed (Secret Manager outage / IAM). The token is dead at Intuit but still persisted — re-run disconnect once the backend is reachable, or re-auth. |

## See also

- [`iam-setup.md`](iam-setup.md) — runtime service account + Secret Manager bindings + Cloud Run invoker IAM.
- [`cloud-run.yaml`](cloud-run.yaml) and [`deploy.sh`](deploy.sh) — deploy artifacts.
- [`docs/qb-service-scope.md`](../docs/qb-service-scope.md) §7 (auth) and §8 (bootstrap/re-auth flow).
