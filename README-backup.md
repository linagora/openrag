
# How to backup OpenRag partition ?

```
docker compose \
    run \
    --build \
    --rm \
    -v /my-backup-dir/:/backup:rw \
    --entrypoint "bash /app/openrag/scripts/entrypoint-backup.sh ${PARTITION_NAME}" \
    openrag-cpu
```
It's better to stop `openrag-cpu` (or `openrag`) service before starting backup.

By default backup script creates plan text uncomressed file. To make things faster you can use multithread compressor the following way:

```
docker compose \
    run \
    --build \
    --rm \
    -v /my-backup-dir/:/backup:rw \
    --entrypoint "bash /app/openrag/scripts/entrypoint-backup-mt.sh ${PARTITION_NAME}" \
    openrag-cpu
```

## Backup all partitions

```bash
docker compose run --build --rm \
  -v ~/backup:/backup:rw \
  --entrypoint "uv run /app/openrag/scripts/backup.py -o /backup/test.openrag" \
  openrag
# Use --include-only to specify the partitions to back up.
```

# How to restore OpenRag partition ?

Start with dry run to ensure the backup file is correct:

```
docker compose \
    run \
    --build \
    --rm \
    -v /my-backup-dir/:/backup:ro \
    --entrypoint "bash /app/openrag/scripts/entrypoint-restore-dry-run.sh backup-file-without-path parition-name" \
    openrag-cpu
```
Backup files are expected to be in `/my-backup-dir/`. If the dry run is successful, run the following script to insert the data :

```
docker compose \
    run \
    --build \
    --rm \
    -v /my-backup-dir/:/backup:ro \
    --entrypoint "bash /app/openrag/scripts/entrypoint-restore.sh backup-file-without-path parition-name" \
    openrag-cpu
```

## Restore all partitions

```bash
docker compose run --build --rm \
  -v ~/backup:/backup:rw \
  --entrypoint "uv run /app/openrag/scripts/restore.py -i /backup/test.openrag" \
  openrag
# Use --include-only to specify the partitions to restore.
```

# Backup dump format

Backups are stored in plain text, with optional xz compression. A backup file consists of multiple sections separated by an empty line.

Each section begins with a single header line, followed by one or more non-empty content lines. Every content line is in JSON format.

There are two types of sections:

* `rdb` – Multiple sections are possible (one section per partition). The first line of an `rdb` section specifies the partition. Each subsequent line represents a single document.
* `vdb` – A single section covering all partitions. One line per chunk.

All `rdb` sections must appear before the `vdb` section.

Example:
```
rdb
{"created": "2025-07-28T16:20:43.144796", "name": "frwiki-nocontext"}
{"created_at": "2025-07-28T16:20:39.612784", "file_id": "10", "file_size": "13.57 KB", "filename": "Algorithmique.txt", "revid": "2962", "source": "/app/data/Algorithmique.txt", "title": "Algorithmique", "url": "https://fr.wikipedia.org/wiki?curid=10"}
{"created_at": "2025-07-28T16:20:40.948772", "file_id": "100", "file_size": "3.79 KB", "filename": "Atoum.txt", "revid": "734387", "source": "/app/data/Atoum.txt", "title": "Atoum", "url": "https://fr.wikipedia.org/wiki?curid=100"}
...

rdb
{"created": "2025-07-10T16:51:25.466016", "name": "enwiki-markdown_splitter-nocontext"}
{"created_at": "2025-07-10T16:51:40.416456", "file_id": "1000", "file_size": "42.03 KB", "filename": "Hercule Poirot.txt", "revid": "25695884", "source": "/app/data/Hercule Poirot.txt", "title": "Hercule Poirot", "url": "https://en.wikipedia.org/wiki?curid=1000"}
{"created_at": "2025-07-10T17:00:02.038049", "file_id": "10000", "file_size": "220.00 B", "filename": "Eiffel.txt", "revid": "5229428", "source": "/app/data/Eiffel.txt", "title": "Eiffel", "url": "https://en.wikipedia.org/wiki?curid=10000"}
...

vdb
{"created_at": "2025-07-28T16:20:39.680783", "file_id": "7", "file_size": "11.01 KB", "filename": "Algèbre linéaire.txt", "page": 1, "partition": "frwiki-nocontext", "revid": "2523928", "source": "/app/data/Algèbre linéaire.txt", "text": "L’algèbre linéaire est ...", "title": "Algèbre linéaire", "url": "https://fr.wikipedia.org/wiki?curid=7", "vector": [0.00012493133544921875, -0.052978515625, ...]}
{"created_at": "2025-07-28T16:20:39.680783", "file_id": "7", "file_size": "11.01 KB", "filename": "Algèbre linéaire.txt", "page": 1, "partition": "frwiki-nocontext", "revid": "2523928", "source": "/app/data/Algèbre linéaire.txt", "text": "Ce n'est qu'au XIXsiècle que ...", "title": "Algèbre linéaire", "url": "https://fr.wikipedia.org/wiki?curid=7", "vector": [-0.0206298828125, -0.09765625, ...]}
...
```

