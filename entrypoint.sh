#!/bin/bash
ENV_ARG=""
if [[ -n "${SHARED_ENV}" ]]; then
  ENV_ARG="--env-file=${SHARED_ENV}"
fi

if [[ "${ENABLE_RAY_SERVE}" == "true" ]]; then
  echo "🔁 Starting with Ray Serve..."
  uv run $ENV_ARG api.py
else
  echo "🚀 Starting with Uvicorn..."
  uv run --no-dev $ENV_ARG uvicorn api:app --host 0.0.0.0 --port ${APP_iPORT:-8080} --reload --workers ${API_NUM_WORKERS:-1}
fi