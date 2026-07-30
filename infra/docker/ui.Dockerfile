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
# compose too. COPY runs as root, then we drop back to a fixed non-root UID.
FROM nginxinc/nginx-unprivileged:1.27-alpine

USER root

COPY --chown=10001:0 --from=build /app/dist /usr/share/nginx/html
COPY --chown=10001:0 infra/compose/nginx/openrag-admin.conf /etc/nginx/conf.d/default.conf

# /var/cache/nginx and /var/run come from the base image (not copied above) —
# own them as 10001:0 and make them group-writable, the same arbitrary-UID
# pattern api.Dockerfile uses (`useradd --gid 0` + `chgrp -R 0` + `chmod g=u`):
# OpenShift's restricted-v2 SCC runs the container as an unpredictable UID from
# the namespace range that is always a member of the root group, so group 0 is
# the only ownership every platform agrees on. The base image already ships
# these paths as 101:0 for exactly that reason — chowning them to a *private*
# group would take that away.
# Re-chowning /etc/nginx/conf.d here as well covers the directory itself (COPY
# --chown above only touched the file), which docker-entrypoint.d/10-listen-on-
# ipv6... needs write access to at startup — without it that script logs
# "can not modify /etc/nginx/conf.d/default.conf (read-only file system?)".
# Don't drop these in favour of the base image's own baked-in permissions: a
# stale/cached base layer silently reverted them once already, and the failures
# that follow surface at request time rather than at startup.

RUN chown -R 10001:0 /var/cache/nginx /etc/nginx/conf.d /var/run && \
    chmod -R g+w /var/cache/nginx /etc/nginx/conf.d /var/run

# Numeric UID:GID, NOT the base image's `nginx` user: that is uid 101 with only
# gid 101 in its group list, so it has neither owner nor group access to the
# paths chowned above. Pinning 10001:0 makes plain docker/compose (which does
# not remap the user) run as the owner, while an OpenShift arbitrary UID still
# writes through group 0. Matches adminUi.podSecurityContext in the Helm chart
# (runAsUser: 10001, runAsGroup: 0).
# Note the mismatch this replaced was latent rather than actively breaking:
# nginx-unprivileged redirects every *_temp_path (and its pid) to /tmp, which is
# 1777, and openrag-admin.conf sets `proxy_cache off`, so nothing writes under
# /var/cache/nginx today. It only bites once something does — a proxy_cache_path,
# an envsubst template landing in conf.d, or a base image that moves the temp
# paths back — and then it fails at request time, not at startup.
USER 10001:0

EXPOSE 8080
