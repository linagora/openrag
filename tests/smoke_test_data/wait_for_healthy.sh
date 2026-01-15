#!/usr/bin/env bash
# Wait for OpenRAG API to be healthy
# Usage: ./wait_for_healthy.sh [URL] [TIMEOUT_SECONDS]

URL="${1:-http://localhost:8080}"
TIMEOUT="${2:-300}"
INTERVAL=10
ELAPSED=0

echo "Waiting for OpenRAG at ${URL} to be healthy (timeout: ${TIMEOUT}s)..."

while [ $ELAPSED -lt $TIMEOUT ]; do
  STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${URL}/health_check" 2>/dev/null)
  if [ "$STATUS_CODE" -eq 200 ]; then
    echo "$(date): API is up and running at ${URL}"
    exit 0
  else
    echo "$(date): Health check failed with status $STATUS_CODE, retrying in ${INTERVAL}s..."
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
  fi
done

echo "ERROR: Timeout waiting for ${URL} to be healthy"
exit 1
