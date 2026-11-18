#!/bin/bash
set -eo pipefail

PARTITION_NAME=$1
BACKUP_DIR=${BACKUP_DIR:-/backup}
OUTPUT_FILE=${BACKUP_DIR}/${PARTITION_NAME}.openrag.mt.xz

# Same rule as backup.py: readable by the owner of the backup folder only. The
# file is created by this script (noclobber refuses any existing name, symlinks
# included) and then only used through descriptor 3, never reopened by name.
umask 077
set -o noclobber
if ! exec 3> "${OUTPUT_FILE}"; then
    echo "Error: File ${OUTPUT_FILE} already exists." >&2
    exit 1
fi
# noclobber still opens an existing FIFO or device: refuse anything that isn't
# the regular file just created.
if [ ! -f /proc/self/fd/3 ]; then
    echo "Error: ${OUTPUT_FILE} is not a regular file." >&2
    exit 1
fi

remove_output() {
    if [ "${OUTPUT_FILE}" -ef /proc/self/fd/3 ]; then
        rm -f "${OUTPUT_FILE}"
    fi
}

if [ "$(id -u)" -eq 0 ] && ! chown --reference="${BACKUP_DIR}" /proc/self/fd/3; then
    remove_output
    exit 1
fi

if ! uv run /app/scripts/backup.py --include-only="${PARTITION_NAME}" --output - | xz -9ec -T 0 --memlimit=20% >&3; then
    remove_output
    exit 1
fi
