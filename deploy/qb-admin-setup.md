# qb-admin — browser-safe OAuth bootstrap service (issue #51)

`qb-admin` is a **public** Cloud Run service that hosts only the Intuit OAuth
bootstrap surface (`/admin/oauth/*`). It exists so a consuming app (e.g. Sample
Manager) can offer a real, in-browser **Connect QuickBooks** button.

## Why this service exists

`qb-service` is IAM-locked at the edge (`--no-allow-unauthenticated`). That is
correct for the data API, but a plain browser cannot attach a Cloud Run identity
token, so Google Frontend answers `403` before the request reaches FastAPI —
both for `/admin/oauth/start` and for Intuit's redirect back to
`/admin/oauth/callback`. A user-facing connect button is therefore impossible
against `qb-service`.

`qb-admin` runs the **same image** as a second, public service that serves the
bootstrap routes only. The data API is turned off on it, and the admin routes
are turned off on `qb-service`, so neither service hosts the other's surface.

| | `qb-service` | `qb-admin` |
|---|---|---|
| Public at edge? | No (`--no-allow-unauthenticated`) | **Yes** (`--allow-unauthenticated`) |
| Routes | `/v1/*` data (+ `/healthz`, `/readyz`) | `/admin/oauth/*` (+ `/healthz`) |
| `QBSVC_ENABLE_DATA_ROUTES` | `true` (default) | **`false`** |
| `QBSVC_ENABLE_ADMIN_ROUTES` | `false` | `true` (default) |
| Admin gate | IAM + `QBSVC_ADMIN_ALLOWLIST` | **launch token** (`QBSVC_ADMIN_LAUNCH_SECRET`) |
| Token store | `mwl-qb-tokens` | **same** `mwl-qb-tokens` |

Because both services read/write the same `mwl-qb-tokens` secret, a connect or
re-auth performed through `qb-admin` is immediately visible to `qb-service` on
its next QBO call.

## The three surfaces (don't confuse them)

| Surface | URL | Auth | Who calls it |
|---|---|---|---|
| **Readiness check** | `GET {qb-service}/readyz` | Cloud Run IAM (ID token) | Server-to-server ops/monitoring. Confirms the proxy + token are usable. |
| **Data API** | `GET/POST/... {qb-service}/v1/*` | Cloud Run IAM (approved service accounts/users) | The consuming app's backend, server-to-server. |
| **Browser OAuth bootstrap** | `{qb-admin}/admin/oauth/start?launch=…` → Intuit → `{qb-admin}/admin/oauth/callback` | App-layer **launch token** | A human admin's browser, via the Connect-QuickBooks button. |

The data API is never reachable from `qb-admin` (data routes are off), and the
bootstrap flow never touches `qb-service`'s locked edge.

## How the launch token works

`/admin/oauth/{start,disconnect}` on `qb-admin` require a signed, short-lived
**launch token**. The consuming app — which already authenticates its own admins
— mints one server-side using the shared secret `QBSVC_ADMIN_LAUNCH_SECRET`
(stored in Secret Manager as `mwl-qb-admin-launch`, and configured identically in
the app). It then renders the button as a link to:

```
{qb-admin}/admin/oauth/start?launch=<token>
```

A visitor without the shared secret cannot forge a token, so a direct tokenless
`GET /admin/oauth/start` returns `403`. The token carries no identity and no
return URL — only an expiry and a signature over it (see
`src/qbsvc/auth/admin_launch.py`). The one-time-ness of the actual handshake is
enforced downstream by the CSRF `state` token, so the launch token only guards
*initiation*. The callback is not launch-gated (Intuit's redirect can't carry a
token); it is protected by the `state` minted at the gated `/start`.

Minting a token is a one-liner the consuming app reproduces in its own language:

```python
# Python reference (mirror in the consuming app):
from qbsvc.auth.admin_launch import mint_launch_token
url = f"{QB_ADMIN}/admin/oauth/start?launch={mint_launch_token(SHARED_SECRET, ttl_seconds=300)}"
```

The scheme is HMAC-SHA256 over the ASCII expiry timestamp, keyed by
`HMAC(secret, b"qbsvc-admin-launch-v1")`, formatted as `"<exp>.<hex-sig>"`.

## The end-to-end flow (what the admin sees)

1. In the consuming app, an authenticated admin clicks **Connect QuickBooks**.
   The app mints a launch token and sends the browser to
   `{qb-admin}/admin/oauth/start?launch=<token>`.
2. `qb-admin` verifies the token and redirects the browser to Intuit's consent
   screen (QuickBooks login).
3. The admin approves. Intuit redirects the browser to
   `{qb-admin}/admin/oauth/callback?code=…&realmId=…&state=…`.
4. `qb-admin` validates `state`, exchanges the code, and writes the rotated
   refresh token to the shared `mwl-qb-tokens` secret.
5. `qb-admin` redirects the browser back to `QBSVC_ADMIN_RETURN_URL`
   (the consuming app), appending `?qb_connected=1&realmId=…` so the app can
   confirm the outcome. **The consuming app never sees the Intuit token** — it
   stays in Secret Manager, read only by `qb-service` and `qb-admin`.

Re-auth / token rotation uses this exact path — the callback overwrites the
stored token (a new Secret Manager version), same as a first connect.

## One-time setup

1. **Create the runtime service account** and grant it read on the secrets:

   ```bash
   gcloud iam service-accounts create qb-admin-runtime \
     --project="$GCP_PROJECT" --display-name="qb-admin OAuth bootstrap runtime"

   for secret in mwl-qb-client-id mwl-qb-client-secret mwl-qb-tokens mwl-qb-admin-launch; do
     gcloud secrets add-iam-policy-binding "$secret" \
       --project="$GCP_PROJECT" \
       --member="serviceAccount:qb-admin-runtime@${GCP_PROJECT}.iam.gserviceaccount.com" \
       --role="roles/secretmanager.secretAccessor"
   done
   ```

   `mwl-qb-tokens` write access (`secretVersionAdder`) is also required so the
   callback can persist the rotated token — grant it the same way if your
   `SecretManagerTokenStore` role split separates read from write.

2. **Create the launch secret** and set the same value in the consuming app:

   ```bash
   openssl rand -base64 48 | gcloud secrets create mwl-qb-admin-launch \
     --project="$GCP_PROJECT" --data-file=-
   ```

## Deploy

```bash
export GCP_PROJECT=your-project-id
export REALM_ID=<intuit-realm-id>
export RETURN_URL=https://sample-manager.example.com/settings/integrations
export INTUIT_ENV=production        # or sandbox
./deploy/qb-admin.deploy.sh
```

Then register `{qb-admin-url}/admin/oauth/callback` as a **redirect URI** in the
Intuit developer console (production keys require a public `https://` URL — this
service is it, replacing the throwaway-tunnel bootstrap in
[`oauth-setup.md`](oauth-setup.md) §3a).

The declarative equivalent is
[`qb-admin.cloud-run.yaml`](qb-admin.cloud-run.yaml) (`gcloud run services
replace`), but note that making the service public is a separate IAM binding the
deploy script applies via `--allow-unauthenticated`.

## Verify

```bash
# Public but gated: a tokenless start must be refused.
curl -sS -o /dev/null -w '%{http_code}\n' "{qb-admin-url}/admin/oauth/start"   # 403

# With a freshly minted launch token it redirects to Intuit (302).
curl -sS -o /dev/null -w '%{http_code}\n' "{qb-admin-url}/admin/oauth/start?launch=<token>"  # 302

# Data API is NOT hosted here.
curl -sS -o /dev/null -w '%{http_code}\n' "{qb-admin-url}/v1/customers"        # 404
```

## Security posture

- **Data API stays locked.** `qb-admin` cannot reach `/v1/*` (data routes off),
  and `qb-service`'s edge is untouched. The public surface is OAuth bootstrap
  only.
- **Non-admins can't connect.** `/admin/oauth/start` and `/disconnect` require an
  unforgeable launch token; the callback requires a valid one-time `state` that
  only a gated `/start` produces.
- **The consuming app never holds the Intuit token.** It only mints launch
  tokens and receives a `qb_connected=1` return; the refresh token lives in
  Secret Manager, exactly as before.
- **Keep the launch TTL short** (`QBSVC_ADMIN_LAUNCH_TTL_SECONDS`, default 300s)
  so a leaked button URL is useless within minutes.

## See also

- [`oauth-setup.md`](oauth-setup.md) — Intuit console setup, the admin gate, and
  the operator/tunnel bootstrap fallback (§3a) this service supersedes for
  production.
- [`iam-setup.md`](iam-setup.md) — runtime service accounts + Secret Manager.
- [`qb-pages-setup.md`](qb-pages-setup.md) — the other public companion (static
  landing/EULA/privacy pages).
