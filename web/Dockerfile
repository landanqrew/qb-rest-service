# syntax=docker/dockerfile:1.7

# qb-pages — public static pages (landing, EULA, privacy policy) that satisfy
# Intuit's production-app URL requirements. See docs/qb-service-scope.md §14
# (Amendment 2): no QBO access, no secrets, no network path to qb-service.
#
# nginx-unprivileged runs as a non-root user (uid 101) and its entrypoint
# renders /etc/nginx/templates/*.template through envsubst, which is how the
# Cloud Run-injected $PORT reaches the listen directive.
FROM nginxinc/nginx-unprivileged:1.27-alpine

# Default for local `docker run`; Cloud Run overrides with its own PORT.
ENV PORT=8080

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY public/ /usr/share/nginx/html/

EXPOSE 8080
