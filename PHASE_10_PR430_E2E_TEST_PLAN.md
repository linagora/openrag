# Phase 10 PR #430 E2E Test Plan

This test plan is for PR #430 before merging into `refactor/hexagonal`.

## Current Automated Result

Latest pulled PR head checked locally: `95ab2cd9`.

Expected automated gates before manual testing:

- Layer guard passes.
- Ruff passes.
- Full pytest passes.
- GitHub checks pass: tests, lint, layer import guard, API tests, Milvus integration, and CodeRabbit.

Local result before this document was added:

- `uv run python scripts/check_layer_imports.py`: passed.
- `uv run ruff check openrag tests/api_tests/test_oidc_lifecycle.py`: passed.
- `uv run pytest`: `1112 passed, 4 skipped`.

## What Manual E2E Should Prove

Manual E2E should prove that Phase 10 did not change product behavior while moving the HTTP boundary into `openrag/api/`.

Verify:

- The server starts from `api.main:app`.
- Health, version, docs, and OpenAPI still load.
- Admin token authentication still works.
- User, partition, workspace, upload/indexing, task status/logs, search, OpenAI-compatible chat, tools, metrics, and actor routes are still reachable from the new routers.
- Failed requests still return the expected HTTP status and JSON error shape.

## Prepare The Branch

From the repo:

```bash
git checkout refactor/phase-10-person-b-api-routers-main
git pull --ff-only origin refactor/phase-10-person-b-api-routers-main
```

Use an environment that can start the full stack. At minimum, define:

```bash
export APP_PORT=8080
export AUTH_TOKEN='or-your-admin-token'
export AUTH_MODE=token
```

If you test search/chat for real, Milvus, Postgres, Ray, and the configured LLM services must be reachable.

## Start The API

Start the API through the new entrypoint:

```bash
./entrypoint.sh
```

Expected result:

- The server listens on `http://localhost:8080`.
- Logs show `api.main:app` or Ray Serve startup through `api.main`.
- No import error references old `main.py` or `routers.*`.

## Smoke Checks

Set helper variables:

```bash
BASE=http://localhost:8080
TOKEN="$AUTH_TOKEN"
PARTITION=phase10-smoke
FILE_ID=phase10-file-001
WORKSPACE=phase10-workspace
```

Run:

```bash
curl -i "$BASE/health_check"
curl -i "$BASE/version"
curl -i "$BASE/openapi.json"
```

Expected result:

- Health and version return HTTP 200.
- OpenAPI returns HTTP 200 JSON.
- OpenAPI tags are readable strings, not tuple-like labels.

## Auth And Admin Routes

Run:

```bash
curl -i -H "Authorization: Bearer $TOKEN" "$BASE/users/info"
curl -i -H "Authorization: Bearer $TOKEN" "$BASE/config"
curl -i -H "Authorization: Bearer bad-token" "$BASE/users/info"
```

Expected result:

- Valid token returns HTTP 200.
- `/config` returns HTTP 200 for admin.
- Bad token returns an auth failure, not a server error.

## Partition And Workspace Flow

Create and inspect a partition:

```bash
curl -i -X POST "$BASE/partition/$PARTITION" \
  -H "Authorization: Bearer $TOKEN"

curl -i "$BASE/partition" \
  -H "Authorization: Bearer $TOKEN"
```

Create and inspect a workspace:

```bash
curl -i -X POST "$BASE/partition/$PARTITION/workspaces" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"workspace_id\":\"$WORKSPACE\",\"display_name\":\"Phase 10 smoke\"}"

curl -i "$BASE/partition/$PARTITION/workspaces" \
  -H "Authorization: Bearer $TOKEN"
```

Expected result:

- Partition creation returns success or a clear already-exists conflict if it was already created.
- Workspace creation returns HTTP 201.
- Listing routes show the created objects.

## Upload And Indexing Flow

Create a small file:

```bash
printf 'Phase 10 smoke document. OpenRAG router migration E2E test.' > /tmp/phase10-smoke.txt
```

Upload it:

```bash
curl -i -X POST "$BASE/indexer/partition/$PARTITION/file/$FILE_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/phase10-smoke.txt;type=text/plain" \
  -F 'metadata={"mimetype":"text/plain"}' \
  -F "workspace_ids=[\"$WORKSPACE\"]"
```

Expected result:

- HTTP 201 or 202 depending on the indexing path.
- Response contains a task id or task status URL.
- No route import or layer error appears in server logs.

Check task status and logs using the returned task id:

```bash
curl -i "$BASE/indexer/task/<TASK_ID>" \
  -H "Authorization: Bearer $TOKEN"

curl -i "$BASE/indexer/task/<TASK_ID>/logs" \
  -H "Authorization: Bearer $TOKEN"
```

Expected result:

- Task reaches `COMPLETED`.
- Logs endpoint returns task-related lines or an empty list, not HTTP 500.

## Search And OpenAI-Compatible Flow

After indexing completes:

```bash
curl -i "$BASE/search/partition/$PARTITION?text=router%20migration&top_k=3&similarity_threshold=0" \
  -H "Authorization: Bearer $TOKEN"
```

Expected result:

- HTTP 200.
- Response has `documents`.
- At least one result should reference `$FILE_ID` when the indexer and vector store are healthy.

If OpenAI-compatible routing is enabled:

```bash
curl -i "$BASE/v1/models" \
  -H "Authorization: Bearer $TOKEN"

curl -i -X POST "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"openrag-$PARTITION\",\"messages\":[{\"role\":\"user\",\"content\":\"What does the Phase 10 smoke document say?\"}],\"stream\":false}"
```

Expected result:

- `/v1/models` returns OpenAI-compatible model objects.
- Chat returns HTTP 200 with an OpenAI-compatible response and sources when retrieval succeeds.

## Admin Utility Routes

Run:

```bash
curl -i "$BASE/queue" \
  -H "Authorization: Bearer $TOKEN"

curl -i "$BASE/actors" \
  -H "Authorization: Bearer $TOKEN"

curl -i "$BASE/metrics" \
  -H "Authorization: Bearer $TOKEN"

curl -i "$BASE/v1/tools" \
  -H "Authorization: Bearer $TOKEN"
```

Expected result:

- Queue, actor, and metrics routes return HTTP 200 when dependencies are available.
- Tools route returns supported conversion/tool information or a clear route-specific error, not an import/layer failure.

## Cleanup

Remove smoke objects if the environment is shared:

```bash
curl -i -X DELETE "$BASE/indexer/partition/$PARTITION/file/$FILE_ID" \
  -H "Authorization: Bearer $TOKEN"

curl -i -X DELETE "$BASE/partition/$PARTITION/workspaces/$WORKSPACE" \
  -H "Authorization: Bearer $TOKEN"

curl -i -X DELETE "$BASE/partition/$PARTITION" \
  -H "Authorization: Bearer $TOKEN"
```

Expected result:

- Cleanup routes return success or a clear not-found response if the object was already removed.

## Pass Criteria

PR #430 is ready for manual approval when:

- Automated checks are green.
- The API starts through `api.main:app`.
- The smoke endpoints return expected statuses.
- Upload/index/search works on a real stack.
- OpenAI-compatible endpoints work when enabled.
- No server log references missing old modules such as `main`, `routers.users`, `routers.utils`, or `routers.openai`.
