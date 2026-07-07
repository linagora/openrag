# Admin UI — static Vite build served by nginx.
# Build context is the repo root (so it can COPY the ui/ directory).
#   docker build -f infra/docker/ui.Dockerfile -t openrag-admin-ui .
FROM node:22-alpine AS build

WORKDIR /app

COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ .

# Drop any local dev env so only the build args below configure the bundle
# (Vite inlines import.meta.env.* at build time — these cannot be set at
# container runtime).
RUN rm -f .env .env.local .env.development .env.development.local

# Empty API base → the SPA makes same-origin relative calls that nginx
# reverse-proxies to the backend (no CORS). VITE_BASE_PATH must match the
# nginx location the SPA is served under.
ARG VITE_API_BASE_URL=""
ARG VITE_BASE_PATH="/app/"
ARG VITE_GRAFANA_URL=""
ARG VITE_APP_NAME="OpenRAG"
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_BASE_PATH=${VITE_BASE_PATH} \
    VITE_GRAFANA_URL=${VITE_GRAFANA_URL} \
    VITE_APP_NAME=${VITE_APP_NAME} \
    VITE_MOCK_API=false

RUN npm run build

# ── Serve with nginx (unprivileged) ───────────────────────────────────────────
# nginx-unprivileged listens on :8080 and runs as a non-root user, so the same
# image runs under a hardened container security context (runAsNonRoot,
# drop ALL capabilities) — required by the Helm chart and good practice in
# compose too. COPY runs as root, then we drop back to the image's non-root user.
FROM nginxinc/nginx-unprivileged:1.27-alpine

USER root

COPY --chown=10001:10001 --from=build /app/dist /usr/share/nginx/html
COPY --chown=10001:10001 infra/compose/nginx/openrag-admin.conf /etc/nginx/conf.d/default.conf

# /var/cache/nginx and /var/run come from the base image (not copied above) —
# own them as 10001:10001 and make them group-writable too, matching the Helm
# chart's fixed podSecurityContext (runAsUser/runAsGroup: 10001). Re-chowning
# /etc/nginx/conf.d here as well covers the directory itself (COPY --chown
# above only touched the file), which docker-entrypoint.d/10-listen-on-ipv6...
# needs write access to at startup. Don't rely on the base image's own
# baked-in permissions instead — a stale/cached base layer silently reverted
# this once already and broke nginx's cache dir at startup (mkdir EACCES on
# /var/cache/nginx/client_temp).

RUN chown -R 10001:10001 /var/cache/nginx /etc/nginx/conf.d /var/run && \
    chmod -R g+w /var/cache/nginx /etc/nginx/conf.d /var/run

USER nginx

EXPOSE 8080
