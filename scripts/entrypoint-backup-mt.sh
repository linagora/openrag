#!/bin/bash

PARTITION_NAME=$1
OUTPUT_FILE=/backup/${PARTITION_NAME}.openrag.mt.xz

if [ -f ${OUTPUT_FILE} ]; then
    echo "Error: File ${OUTPUT_FILE} already exists." > &2
    exit 1
fi

uv run /app/scripts/backup.py --include-only=${PARTITION_NAME} --output - | xz -9ec -T 0 --memlimit=20% > ${OUTPUT_FILE}
