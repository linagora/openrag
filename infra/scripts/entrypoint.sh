#!/bin/bash

# --- Writable-dir permission fix + privilege drop --------------------------
# The app runs as a non-root user whose primary group is GID 0. Its writable
# dirs (/app/data, /app/logs, /app/model_weights) are bind-mounted from the
# host, so the image's group-0-writable perms (Dockerfile `chmod g=u`) don't
# apply to them — the host directory's ownership does, and Docker creates a
# missing bind source as root:root with no group-write. That makes the
# non-root app fail with "Permission denied" on upload/logging/model download.
#
# To make bind mounts "just work" with no host-side chmod: when started as
# root (compose sets `user: "0:0"` on the service), grant GID-0 write on those
# dirs — mirroring the image's own `chmod g=u` — then drop to the non-root app
# user and continue. When NOT root (OpenShift assigns an arbitrary non-root
# UID in GID 0; or a plain non-root run), skip the fix: the platform's GID-0
# membership plus the image's group-writable paths already cover it.
APP_UID="${APP_UID:-10001}"
if [ "$(id -u)" = "0" ]; then
  # /app/openrag/.chainlit/translations can be a bind mount (compose maps the
  # host i8n/ folder there for custom translations). Like the dirs below, a
  # host-owned mount keeps its host ownership, so Chainlit — which writes any
  # bundled language file missing from the mount on startup — hits
  # PermissionError as the non-root app user. Grant GID-0 write here too.
  for d in /app/data /app/logs /app/model_weights /app/openrag/.chainlit/translations; do
    mkdir -p "$d" 2>/dev/null || true
    chgrp -R 0 "$d" 2>/dev/null || true
    chmod -R g+rwX "$d" 2>/dev/null || true
  done
  # Re-exec this script as the non-root app user (UID in GID 0) to run the app.
  exec setpriv --reuid "$APP_UID" --regid 0 --clear-groups /app/entrypoint.sh "$@"
fi

ENV_ARGS=()
if [[ -n "${SHARED_ENV}" ]]; then
  ENV_ARGS+=("--env-file=${SHARED_ENV}")
fi

if [[ "${ENABLE_RAY_SERVE}" == "true" ]]; then
  echo "🔁 Starting with Ray Serve..."
  uv run --no-dev "${ENV_ARGS[@]}" -m api.main
else
  echo "🚀 Starting with Uvicorn..."
  # This path always runs a SINGLE uvicorn worker. The app initializes Ray and
  # its named actors (Indexer, Vectordb, TaskStateManager, ...) at import time,
  # so each extra worker would be a separate process starting its own isolated
  # Ray cluster with duplicate actors — fragmenting task state and the vector
  # DB. Concurrency comes from the async app + Ray, not from uvicorn workers.
  # To scale the HTTP layer horizontally, use Ray Serve (ENABLE_RAY_SERVE=true,
  # RAY_SERVE_NUM_REPLICAS=N), which runs N replicas inside one Ray cluster.
  if [[ -n "${API_NUM_WORKERS}" && "${API_NUM_WORKERS}" != "1" ]]; then
    echo "⚠️  API_NUM_WORKERS=${API_NUM_WORKERS} is ignored: this app runs a single uvicorn worker (Ray provides concurrency). To scale, set ENABLE_RAY_SERVE=true with RAY_SERVE_NUM_REPLICAS." >&2
  fi
  # --reload is dev-only (set UVICORN_RELOAD=true); it also forces a single worker.
  RELOAD_ARGS=()
  if [[ "${UVICORN_RELOAD}" == "true" ]]; then
    RELOAD_ARGS+=("--reload")
  fi
  # uvicorn only honours X-Forwarded-* from peers listed in --forwarded-allow-ips
  # (default: 127.0.0.1), so a reverse proxy outside loopback — the admin-ui
  # container, a k8s ingress — is ignored: request.client.host stays the proxy's
  # address (collapsing every user into one rate-limit bucket) and
  # X-Forwarded-Proto is dropped. Forward the same UVICORN_FORWARDED_ALLOW_IPS
  # that api.main's __main__ block reads, so one documented variable covers both
  # entrypoints — the bare `uvicorn` CLI otherwise only reads FORWARDED_ALLOW_IPS.
  uv run --no-dev "${ENV_ARGS[@]}" uvicorn api.main:app --host 0.0.0.0 --port "${APP_iPORT:-8080}" "${RELOAD_ARGS[@]}" --workers 1 \
    --proxy-headers --forwarded-allow-ips "${UVICORN_FORWARDED_ALLOW_IPS:-127.0.0.1}"
fi
