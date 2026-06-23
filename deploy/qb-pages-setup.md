# qb-pages — public static-pages companion service

`qb-pages` is a second, intentionally tiny Cloud Run service that exists for one
reason: to satisfy Intuit's production-app requirements with publicly resolvable
**landing**, **EULA**, and **privacy-policy** URLs. `qb-service` itself is
IAM-locked at the edge (`--no-allow-unauthenticated`, see
[`oauth-setup.md`](oauth-setup.md) and the scope doc §7) and therefore cannot
host anything Intuit's reviewers can reach. `qb-pages` fills that gap and
nothing else.

## Hard constraints (scope doc Amendment 2)

`qb-pages` is the deliberate opposite of `qb-service`:

| | `qb-service` | `qb-pages` |
|---|---|---|
| Public? | No (IAM-locked) | **Yes** (`--allow-unauthenticated`) |
| QBO access | Yes | **None** |
| Secrets | Secret Manager | **None** |
| Service-account IAM grants | `secretAccessor` on 3 secrets | **Zero** |
| Network path to `qb-service` | n/a | **None** |
| Blast radius if compromised | the integration | **three HTML files** |

The whole point is that "public" only ever means three static HTML files.

## What's in the box

```
web/
├── index.html                  # Landing page: names the app + its purpose
├── eula.html                   # EULA  (template — needs operator review)
├── privacy.html                # Privacy policy (template — needs operator review)
├── nginx/default.conf.template # Serves /, /eula, /privacy, /healthz; 404 else
├── Dockerfile                  # nginx:alpine static server, honors $PORT
└── .dockerignore               # Build context limited to the files above
```

> **Template legal text:** `eula.html` and `privacy.html` are generic,
> operator-neutral boilerplate for an internal, single-tenant business
> integration. **Any operator forking this repo must review and approve them
> before submitting their app to Intuit for production** — the pages themselves
> carry no draft/template banner, so this review is on you.

## One-time setup

1. **Create the permission-less runtime service account** (grant it nothing):

   ```bash
   gcloud iam service-accounts create qb-pages-runtime \
     --project="$GCP_PROJECT" \
     --display-name="qb-pages public static site (no permissions)"
   ```

   Do **not** attach any IAM roles to it. It exists only so `qb-pages` does not
   run as the default compute service account.

2. **Create the Artifact Registry repo** (if it doesn't exist):

   ```bash
   gcloud artifacts repositories create qb-pages \
     --project="$GCP_PROJECT" --location="$REGION" --repository-format=docker
   ```

## Deploy

```bash
export GCP_PROJECT=your-project-id
export REGION=us-central1          # optional, this is the default
./deploy/qb-pages.deploy.sh
```

The script builds the image from `web/` **only** (the `.dockerignore` keeps the
rest of the repo out of the build context), then deploys with
`--allow-unauthenticated`, the dedicated permission-less service account, no
secrets, and no QBO env vars. `nginx` reads the port from `$PORT`, which Cloud
Run injects automatically.

The declarative equivalent is [`qb-pages.cloud-run.yaml`](qb-pages.cloud-run.yaml)
(`gcloud run services replace`), but note that making the service public is a
separate IAM binding (`allUsers` → `roles/run.invoker`) that the deploy script
applies via `--allow-unauthenticated`.

## After deploy

The script prints the three public URLs. Register them in the Intuit developer
console (Host/launch URL, EULA URL, Privacy policy URL):

```
https://qb-pages-XXXX.a.run.app/
https://qb-pages-XXXX.a.run.app/eula
https://qb-pages-XXXX.a.run.app/privacy
```

Smoke test (no auth needed — these are public):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://qb-pages-XXXX.a.run.app/
curl -sS -o /dev/null -w '%{http_code}\n' https://qb-pages-XXXX.a.run.app/eula
curl -sS -o /dev/null -w '%{http_code}\n' https://qb-pages-XXXX.a.run.app/privacy
```

## Local verification

Build and run the container, then curl the three pages:

```bash
docker build -t qb-pages ./web
docker run --rm -p 8080:8080 qb-pages
# in another shell:
curl -i localhost:8080/         # 200, landing page
curl -i localhost:8080/eula     # 200, EULA draft
curl -i localhost:8080/privacy  # 200, privacy draft
```

The Python test `tests/test_qb_pages.py` exercises this same nginx config
directly (it runs `nginx` against `web/nginx/default.conf.template`), so the
serving behavior is covered by the suite even where the container registry is
unreachable.
