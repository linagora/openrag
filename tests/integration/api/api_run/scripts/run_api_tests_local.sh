#!/usr/bin/env bash
set -euo pipefail

# Local replica of .github/workflows/api_tests.yml
# Builds/starts the stack (mock-vllm included), waits for health, runs tests, then cleans up.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_DIR="$ROOT_DIR"

cd "$COMPOSE_DIR"

echo "[1/4] Building and starting services"
docker compose build
docker compose up -d

echo "[2/4] Waiting for services to be ready..."

# Wait for mock-vllm
echo "Waiting for mock-vllm..."
if ! timeout 60 bash -c 'until curl -sf http://localhost:8000/health 2>/dev/null; do sleep 2; done'; then
  echo "Mock VLLM failed to start"
  docker compose logs mock-vllm
  docker compose down -v
  exit 1
fi

# Wait for OpenRAG API
echo "Waiting for OpenRAG API..."
for i in {1..60}; do
  if curl -sf http://localhost:8080/health_check 2>/dev/null; then
    echo "OpenRAG API ready after $i attempts"
    break
  fi
  echo "Attempt $i/60 - waiting..."
  sleep 5
done

if ! curl -sf http://localhost:8080/health_check; then
  echo "OpenRAG API failed to start"
  docker compose logs openrag
  docker compose down -v
  exit 1
fi

echo "[3/4] Running API tests"
cd "$ROOT_DIR"
# Ensure test deps are present (mirror CI)
if ! python3 - <<'PY'
import httpx
import pytest  # noqa: F401
import pytest_timeout  # noqa: F401
PY
then
  echo "Installing pytest + httpx + pytest-timeout..."
  pip install pytest httpx pytest-timeout
fi
OPENRAG_API_URL=http://localhost:8080 python3 -m pytest ../  -v --timeout=120 --tb=short

echo "[4/4] Cleaning up"
cd "$COMPOSE_DIR"
docker compose down -v

echo "Done."
