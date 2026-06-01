#!/bin/bash

PARTITION_NAME=$1
OUTPUT_FILE=/backup/${PARTITION_NAME}.openrag

uv run /app/scripts/backup.py --include-only=${PARTITION_NAME} --output ${OUTPUT_FILE}
