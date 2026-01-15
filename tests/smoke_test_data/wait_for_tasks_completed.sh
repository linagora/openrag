#!/usr/bin/env bash
# Wait for all indexing tasks to complete
# Usage: ./wait_for_tasks_completed.sh [URL] [EXPECTED_COUNT] [TIMEOUT_SECONDS]

URL="${1:-http://localhost:8080}"
NUM="${2:-500}"
TIMEOUT="${3:-1800}"
INTERVAL=10
ELAPSED=0

echo "Waiting for ${NUM} tasks to complete at ${URL} (timeout: ${TIMEOUT}s)..."

while [ $ELAPSED -lt $TIMEOUT ]; do
  RESPONSE=$(curl -fs "${URL}/queue/info" 2>/dev/null)

  if [ -z "$RESPONSE" ]; then
    echo "$(date): Failed to fetch queue info, retrying..."
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
    continue
  fi

  TOTAL_FAILED=$(echo "$RESPONSE" | jq '.tasks.total_failed')
  TOTAL_COMPLETED=$(echo "$RESPONSE" | jq '.tasks.total_completed')

  if [ "$TOTAL_FAILED" -ne 0 ]; then
    echo "ERROR: ${TOTAL_FAILED} tasks failed. Aborting."
    echo "$RESPONSE" | jq
    exit 1
  fi

  if [ "$TOTAL_COMPLETED" -eq "$NUM" ]; then
    echo "$(date): All ${TOTAL_COMPLETED} tasks completed successfully."
    exit 0
  fi

  echo "$(date): ${TOTAL_COMPLETED}/${NUM} tasks completed, ${TOTAL_FAILED} failed"
  sleep $INTERVAL
  ELAPSED=$((ELAPSED + INTERVAL))
done

echo "ERROR: Timeout waiting for tasks to complete"
exit 1
