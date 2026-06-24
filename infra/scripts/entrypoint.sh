#!/bin/bash
ENV_ARGS=()
if [[ -n "${SHARED_ENV}" ]]; then
  ENV_ARGS+=("--env-file=${SHARED_ENV}")
fi

if [[ "${ENABLE_RAY_SERVE}" == "true" ]]; then
  echo "🔁 Starting with Ray Serve..."
  uv run "${ENV_ARGS[@]}" -m api.main
else
  echo "🚀 Starting with Uvicorn..."
  # --reload is a development feature (watches/auto-imports source on change)
  # and is incompatible with multiple workers. Enable it only with
  # UVICORN_RELOAD=true for local dev; production runs without it.
  RELOAD_ARGS=()
  WORKERS="${API_NUM_WORKERS:-1}"
  if [[ "${UVICORN_RELOAD}" == "true" ]]; then
    RELOAD_ARGS+=("--reload")
    # uvicorn rejects --reload together with multiple workers; force single worker.
    WORKERS="1"
  fi
  uv run --no-dev "${ENV_ARGS[@]}" uvicorn api.main:app --host 0.0.0.0 --port "${APP_iPORT:-8080}" "${RELOAD_ARGS[@]}" --workers "${WORKERS}"
fi
