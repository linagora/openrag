#!/usr/bin/env bash
set -euo pipefail

ENDPOINT_URL=$1
PARTITION_NAME=$2
FILE_NAME=$3
PARENT_FILE_NAME=$4
THREAD_ID=$5
CONTENT=$(cat)

metadata=$(jq -nc \
  --arg parent_file_name "${PARENT_FILE_NAME}" \
  --arg thread_id "${THREAD_ID}" \
  '{
    mimetype: "text/plain",
    "email.subject": "subject",
    datetime: "datetime",
    parent_id: $parent_file_name,
    relationship_id: $thread_id,
    doctype: "com.linagora.email",
    "email.preview": "e-mail preview"
  }')

echo ${metadata}

curl -X 'POST' \
  ${ENDPOINT_URL}/indexer/partition/${PARTITION_NAME}/file/${FILE_NAME} \
  -H 'accept: application/json' \
  -H 'Content-Type: multipart/form-data' \
  -F "file=@-;filename=${FILE_NAME};type=text/plain" \
  -F "metadata=$metadata" <<< "$CONTENT"

