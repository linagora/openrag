#!/usr/bin/env bash
set -euo pipefail

ENDPOINT_URL=$1
PARTITION_NAME=$2
FILE_NAME=$3
CONTENT=$(cat)

curl -X 'POST' \
  ${ENDPOINT_URL}/indexer/partition/${PARTITION_NAME}/file/${FILE_NAME} \
  -H 'accept: application/json' \
  -H 'Content-Type: multipart/form-data' \
  -F "file=@-;filename=${FILE_NAME};type=text/plain" \
  -F 'metadata={"mimetype":"text/plain"}' <<< "$CONTENT"

