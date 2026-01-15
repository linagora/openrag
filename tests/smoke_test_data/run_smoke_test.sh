#!/usr/bin/env bash
# Run smoke test: index 500 SimplWiki documents and wait for completion
# Usage: ./run_smoke_test.sh [URL]
#
# Prerequisites:
#   - OpenRAG server running and healthy
#   - jq installed
#   - Python with httpx installed (for data_indexer.py)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
URL="${1:-http://localhost:8080}"
DATA_DIR="${SCRIPT_DIR}/simplewiki-500"
PARTITION="simplewiki-500"

echo "=== OpenRAG Smoke Test ==="
echo "URL: ${URL}"
echo "Data: ${DATA_DIR}"
echo "Partition: ${PARTITION}"
echo ""

# Step 1: Wait for API to be healthy
echo "Step 1: Checking API health..."
"${SCRIPT_DIR}/wait_for_healthy.sh" "${URL}" 120

# Step 2: Index documents
echo ""
echo "Step 2: Indexing 500 documents..."
python3 "${PROJECT_ROOT}/utility/data_indexer.py" \
    -u "${URL}" \
    -d "${DATA_DIR}" \
    -p "${PARTITION}"

# Step 3: Wait for all tasks to complete
echo ""
echo "Step 3: Waiting for indexing tasks to complete..."
"${SCRIPT_DIR}/wait_for_tasks_completed.sh" "${URL}" 500 1800

echo ""
echo "=== Smoke test completed successfully ==="
