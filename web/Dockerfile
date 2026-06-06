# qb-pages — the companion public static-pages service.
#
# Deliberately minimal: an nginx static file server hosting exactly three HTML
# pages (landing, EULA, privacy) so Intuit's production-app review has real,
# publicly resolvable URLs to point at. It has no QBO access, no credentials, no
# environment configuration beyond the port, and no network path to qb-service.
#
# nginx's entrypoint runs envsubst over /etc/nginx/templates/*.template at
# startup, so ${PORT} in the config is filled from the port Cloud Run injects
# (default 8080). Nothing else is templated.

# Pinned to the 1.27 minor tag rather than a sha256 digest on purpose: this is a
# public-facing nginx, so picking up patch-level security fixes on each rebuild
# is desirable. Reproducibility of a given deploy is provided downstream — the
# qb-pages image is tagged by git SHA and stored in Artifact Registry (see
# deploy/qb-pages.deploy.sh).
FROM nginx:1.27-alpine

# Default for local `docker run`; Cloud Run overrides $PORT at deploy time.
ENV PORT=8080

# Only PORT should be substituted into the template — leave everything else
# (and nginx's own runtime variables) alone.
ENV NGINX_ENVSUBST_FILTER=PORT

# Static pages -> docroot.
COPY index.html eula.html privacy.html /usr/share/nginx/html/

# Server config template -> rendered to /etc/nginx/conf.d/default.conf at start.
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 8080

# Inherit nginx:alpine's entrypoint (envsubst + nginx -g 'daemon off;').
