#!/usr/bin/env bash
set -euo pipefail

ENDPOINT_URL=$1
PARTITION_NAME=$2
QUERY=$(cat)

payload=$(jq -nc \
  --arg model "openrag-${PARTITION_NAME}" \
  --arg query "${QUERY}" \
  '{
    model: $model,
    messages: [{role: "user", content: $query}],
    temperature: 0.3,
    top_p: 1,
    stream: false,
    max_tokens: 1024,
    logprobs: 0,
    metadata: {use_map_reduce: false}
  }')

#echo ${payload}

response=`curl -X POST "${ENDPOINT_URL}/v1/chat/completions" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d "$payload"`

#echo $response | jq .

extra=`echo $response | jq '.extra | fromjson'`

echo $extra | jq .

