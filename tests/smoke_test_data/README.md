# Smoke Test Data & Scripts

This directory contains test data and scripts for running smoke tests against OpenRAG.

## Contents

- `simplewiki-500/` - 500 text files from Simple English Wikipedia
- `run_smoke_test.sh` - Main script to run the full smoke test (indexing)
- `run_backup_restore_test.sh` - Test backup/restore with data integrity verification
- `wait_for_healthy.sh` - Wait for OpenRAG API to be healthy
- `wait_for_tasks_completed.sh` - Wait for indexing tasks to complete

## Quick Start

```bash
# 1. Start OpenRAG services
docker compose --profile cpu up -d

# 2. Run smoke test (indexes 500 documents)
./tests/smoke_test_data/run_smoke_test.sh http://localhost:8080

# 3. Run backup/restore test (requires indexed data from step 2)
./tests/smoke_test_data/run_backup_restore_test.sh simplewiki-500
```

## Scripts

### run_smoke_test.sh

Indexes 500 SimplWiki documents and waits for completion.

```bash
# With default URL (http://localhost:8080)
./run_smoke_test.sh

# With custom URL
./run_smoke_test.sh http://your-openrag-server:8080
```

**What it does:**
1. Checks API health at `/health_check`
2. Indexes 500 documents to partition `simplewiki-500` using `utility/data_indexer.py`
3. Waits for all indexing tasks to complete via `/queue/info`

**Prerequisites:**
- OpenRAG server running
- `jq` installed
- Python with `httpx` for `data_indexer.py`

### run_backup_restore_test.sh

Tests backup/restore functionality with data integrity verification.

```bash
# Test with default partition (simplewiki-500)
./run_backup_restore_test.sh

# Test with custom partition
./run_backup_restore_test.sh my-partition
```

**What it does:**
1. Creates backup of the partition → `{partition}.openrag`
2. Modifies backup to change partition name → `{partition}-restored`
3. Restores modified backup to new partition
4. Creates backup of restored partition
5. Compares both backups (excluding timestamps) to verify data integrity

**Prerequisites:**
- OpenRAG services running via `docker compose`
- Partition with indexed documents (run `run_smoke_test.sh` first)
- `jq` installed

**What it validates:**
| Aspect | Description |
|--------|-------------|
| Backup works | `backup.py` correctly exports RDB + VDB data |
| Restore works | `restore.py` correctly imports data |
| Data integrity | Round-trip comparison proves no data loss |
| Partition migration | Can restore to a different partition name |

### wait_for_healthy.sh

Wait for OpenRAG API to be healthy.

```bash
# Default: http://localhost:8080, 300s timeout
./wait_for_healthy.sh

# Custom URL and timeout
./wait_for_healthy.sh http://localhost:8080 120
```

### wait_for_tasks_completed.sh

Wait for indexing tasks to complete.

```bash
# Default: 500 tasks, 1800s timeout
./wait_for_tasks_completed.sh http://localhost:8080

# Custom task count and timeout
./wait_for_tasks_completed.sh http://localhost:8080 100 600
```

## Full Test Workflow

To run the complete smoke test suite (indexing + backup/restore):

```bash
#!/bin/bash
set -e

# Start services
docker compose --profile cpu up -d

# Wait for services
./tests/smoke_test_data/wait_for_healthy.sh http://localhost:8080

# Run indexing test
./tests/smoke_test_data/run_smoke_test.sh http://localhost:8080

# Run backup/restore test
./tests/smoke_test_data/run_backup_restore_test.sh simplewiki-500

echo "All tests passed!"
```

## Backup File Format

Backups are stored in `.openrag` format (plain text or `.xz` compressed):

```
rdb
{"created": "2025-07-28T16:20:43", "name": "partition-name"}
{"file_id": "1", "filename": "doc1.txt", ...}
{"file_id": "2", "filename": "doc2.txt", ...}

vdb
{"file_id": "1", "partition": "partition-name", "text": "...", "vector": [...]}
{"file_id": "1", "partition": "partition-name", "text": "...", "vector": [...]}
```

- `rdb` sections: Partition metadata and document info (from PostgreSQL)
- `vdb` section: Chunks with embeddings (from Milvus)

See `docs/content/docs/documentation/backup_restore.md` for more details.
