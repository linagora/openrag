#!/bin/bash
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
  uv run --no-dev "${ENV_ARGS[@]}" uvicorn api.main:app --host 0.0.0.0 --port "${APP_iPORT:-8080}" "${RELOAD_ARGS[@]}" --workers 1
fi
