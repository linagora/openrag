#!/bin/bash

PARTITION_NAME=$1
OUTPUT_FILE=/backup/${PARTITION_NAME}.openrag.mt.xz

if [ -f ${OUTPUT_FILE} ]; then
    echo "Error: File ${OUTPUT_FILE} already exists." >&2
    exit 1
fi

# Same rule as backup.py: readable by the owner of the backup folder only.
install -m 600 /dev/null "${OUTPUT_FILE}"
chown --reference=/backup "${OUTPUT_FILE}"

uv run /app/scripts/backup.py --include-only=${PARTITION_NAME} --output - | xz -9ec -T 0 --memlimit=20% > ${OUTPUT_FILE}
