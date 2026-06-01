#!/bin/bash
ENV_ARG=""
if [[ -n "${SHARED_ENV}" ]]; then
  ENV_ARG="--env-file=${SHARED_ENV}"
fi

export PYTHONPATH="/app:/app/openrag:${PYTHONPATH}"

start_rag_audit_cron() {
  if [[ "${RAG_AUDIT_CRON_ENABLED:-true}" != "true" ]]; then
    return
  fi

  local schedule="${RAG_AUDIT_CRON_SCHEDULE:-0 0 * * *}"
  local timezone="${RAG_AUDIT_CRON_TZ:-Europe/Paris}"
  local command="cd /app/openrag && uv run --no-dev $ENV_ARG python -m rag_audit.openrag_job >> /app/logs/rag_audit_cron.log 2>&1"

  mkdir -p /app/logs
  export -p > /tmp/openrag_cron_env.sh
  printf '#!/bin/bash\nsource /tmp/openrag_cron_env.sh\nexport PYTHONPATH="%s"\n%s\n' "${PYTHONPATH}" "${command}" > /tmp/run_rag_audit.sh
  chmod +x /tmp/run_rag_audit.sh
  printf 'SHELL=/bin/bash\nPATH=%s\nCRON_TZ=%s\n%s /tmp/run_rag_audit.sh\n' "${PATH}" "${timezone}" "${schedule}" > /tmp/rag_audit_crontab
  crontab /tmp/rag_audit_crontab
  cron
  echo "RAG audit cron scheduled: ${schedule} (${timezone})"
}

start_rag_audit_cron

if [[ "${ENABLE_RAY_SERVE}" == "true" ]]; then
  echo "🔁 Starting with Ray Serve..."
  uv run $ENV_ARG api.py
else
  echo "🚀 Starting with Uvicorn..."
  uv run --no-dev $ENV_ARG uvicorn api:app --host 0.0.0.0 --port ${APP_iPORT:-8080} --reload --workers ${API_NUM_WORKERS:-1}
fi
