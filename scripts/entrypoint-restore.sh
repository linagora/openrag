#!/bin/bash

PARTITION_NAME=$1
BACKUP_FILE=$2

uv run /app/scripts/restore.py --include-only=${PARTITION_NAME} --batch-size 8192 ${BACKUP_FILE}
