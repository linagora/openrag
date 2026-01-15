#!/usr/bin/env bash
# Test backup/restore functionality with data integrity verification
# Usage: ./run_backup_restore_test.sh [PARTITION_NAME]
#
# Prerequisites:
#   - OpenRAG services running via docker compose
#   - Partition with indexed documents (run run_smoke_test.sh first)
#   - jq installed
#
# This test validates:
#   1. Backup creation works correctly
#   2. Restore to a new partition works
#   3. Data integrity is preserved (round-trip comparison)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PARTITION="${1:-simplewiki-500}"
PARTITION_RESTORED="${PARTITION}-restored"
BACKUP_DIR="${PROJECT_ROOT}/backup_test"
ENV_FILE="${PROJECT_ROOT}/.env"

echo "=== OpenRAG Backup/Restore Test ==="
echo "Partition: ${PARTITION}"
echo "Backup dir: ${BACKUP_DIR}"
echo ""

# Cleanup function
cleanup() {
    echo "Cleaning up..."
    rm -rf "${BACKUP_DIR}"
}

# Setup
mkdir -p "${BACKUP_DIR}"

# Step 1: Create backup of original partition
echo "Step 1: Creating backup of partition '${PARTITION}'..."
docker compose --env-file "${ENV_FILE}" run --rm \
    -v "${BACKUP_DIR}:/backup:rw" \
    --entrypoint "bash /app/openrag/scripts/entrypoint-backup.sh ${PARTITION}" \
    openrag-cpu

BACKUP_FILE="${BACKUP_DIR}/${PARTITION}.openrag"
if [ ! -f "${BACKUP_FILE}" ]; then
    echo "ERROR: Backup file not created: ${BACKUP_FILE}"
    cleanup
    exit 1
fi

LINES_ORIGINAL=$(wc -l < "${BACKUP_FILE}")
echo "Backup created: ${BACKUP_FILE} (${LINES_ORIGINAL} lines)"

# Step 2: Modify backup to change partition name
echo ""
echo "Step 2: Modifying backup (changing partition name to '${PARTITION_RESTORED}')..."
MODIFIED_BACKUP="${BACKUP_DIR}/modified.openrag"
sed "s/\"${PARTITION}\"/\"${PARTITION_RESTORED}\"/g" "${BACKUP_FILE}" > "${MODIFIED_BACKUP}"

# Step 3: Restore to new partition
echo ""
echo "Step 3: Restoring backup to new partition '${PARTITION_RESTORED}'..."
docker compose --env-file "${ENV_FILE}" run --rm \
    -v "${BACKUP_DIR}:/backup:ro" \
    --entrypoint "bash /app/openrag/scripts/entrypoint-restore.sh ${PARTITION_RESTORED} /backup/modified.openrag" \
    openrag-cpu

echo "Restore completed"

# Step 4: Create backup of restored partition
echo ""
echo "Step 4: Creating backup of restored partition '${PARTITION_RESTORED}'..."
docker compose --env-file "${ENV_FILE}" run --rm \
    -v "${BACKUP_DIR}:/backup:rw" \
    --entrypoint "bash /app/openrag/scripts/entrypoint-backup.sh ${PARTITION_RESTORED}" \
    openrag-cpu

RESTORED_BACKUP="${BACKUP_DIR}/${PARTITION_RESTORED}.openrag"
if [ ! -f "${RESTORED_BACKUP}" ]; then
    echo "ERROR: Restored backup file not created: ${RESTORED_BACKUP}"
    cleanup
    exit 1
fi

LINES_RESTORED=$(wc -l < "${RESTORED_BACKUP}")
echo "Backup created: ${RESTORED_BACKUP} (${LINES_RESTORED} lines)"

# Step 5: Compare backups (normalize partition names and remove timestamps)
echo ""
echo "Step 5: Comparing backups for data integrity..."

# Normalize: change restored partition name back and remove 'created' timestamps
NORMALIZED_ORIGINAL="${BACKUP_DIR}/normalized_original.txt"
NORMALIZED_RESTORED="${BACKUP_DIR}/normalized_restored.txt"

grep -Ev '^{"created": ' "${BACKUP_FILE}" > "${NORMALIZED_ORIGINAL}"
sed "s/\"${PARTITION_RESTORED}\"/\"${PARTITION}\"/g" "${RESTORED_BACKUP}" | grep -Ev '^{"created": ' > "${NORMALIZED_RESTORED}"

if diff "${NORMALIZED_ORIGINAL}" "${NORMALIZED_RESTORED}" > /dev/null; then
    echo "SUCCESS: Backups are identical (data integrity verified)"
else
    echo "ERROR: Backups differ! Data integrity check failed."
    echo "Differences:"
    diff "${NORMALIZED_ORIGINAL}" "${NORMALIZED_RESTORED}" | head -50
    cleanup
    exit 1
fi

# Cleanup
cleanup

echo ""
echo "=== Backup/Restore test completed successfully ==="
