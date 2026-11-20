# Indexing refactor: decisions

Choices made in the indexing refactor (#1428, #1429, #1430, #1431 and the issues that follow) that the issues leave open, or that depart from them. One file for the whole refactor.

**Adding one:** append `D<n>` and never renumber. Name the issue or PR it belongs to. Keep it to a few lines: decision, why, what was rejected, impact.

**Status:**

- `taken`: done, not yet confirmed;
- `confirmed`: say by whom;
- `reversed`: link the decision that replaces it.

---

## D1. The parse suffix is part of the key

**#1429**, `object_keys.py` (#1470). Status: taken.

- **Decision:** keys end with `{upload_id}{suffix}` (`…/0123…cdef.pdf`). The suffix is checked as a dot plus 1 to 16 letters or digits. The filename stays out of the key.
- **Why:** on the filesystem, `local_copy` must yield the stored file itself, not a copy, *and* keep the suffix the file is parsed as. Both hold only if the stored path ends with the suffix.
- **Rejected:** symlinking or copying to a suffixed path on every parse.

## D2. A partition or file id over 191 bytes is refused (400)

**#1429**, `object_keys.py` (#1470). Takes effect once uploads go through the port. Status: taken.

- **Decision:** 191 bytes encode to 255, the limit for one filesystem path component.
- **Why:** the key stays reversible and simple.
- **Rejected:**
  - hashing a component that is too long (the key no longer gives the id back);
  - splitting it over several segments (harder to parse).
- **Impact:** a new refusal, since nothing limits these ids today; it goes in the v2.4.0 notes. Twake isn't affected: it sends the instance domain and a 32-hex CouchDB id (cozy-stack fe2912c6f, `model/rag/index.go`).

## D3. With `fs`, new uploads use the key layout and older files stay in place

**#1429**, `legacy_key` and the filesystem adapter (#1470); shapes how uploads move onto the port. Status: taken, to confirm.

- **Decision:**
  - new files go to `data_dir/<key>`;
  - files uploaded before stay where they are, and the adapter maps `legacy/<path>` to `data_dir/<path>` with nothing copied;
  - `originals`, `uploads`, `tmp` and `legacy` are reserved at the top of `data_dir`.
- **Why:** #1429 keeps `fs` as it is today, with no behavior change. We read that as the same volume, not the same layout. It also means no migration on `fs`.
- **Impact (checked on develop c7643617a):** nothing breaks. Parsers, sidecars, cleanup and type detection use the path or its suffix. Displayed names, Chainlit and Twake read `filename`, never `source`. No existing upload uses a reserved name. **The change that moves uploads onto the port must** send the catalog `filename` as the `/static` "Save as" name, in the same PR that writes the new layout: `download.py:95` uses the path's name, which would become `<uuid>.pdf`. Rolling back past that change is safe, because a new `source` is still a valid path under `data_dir`.
- **To confirm:** the new layout is live under the defaults once uploads go through the port, while the refactor's other changes are inactive with the defaults. Users see no change, but operators see files in new places.

## D4. The port has `stat`, `put_bytes` and metadata rules

**#1429**, `core/ports/object_store.py` (#1470). Status: taken.

- **Decision:**
  - **Added:**
    - `stat(key) -> ObjectInfo` (size, SHA-256 or `None`, metadata);
    - `put_bytes`, a helper over `put_stream`;
    - `open_stream` as an async context manager.
  - **Metadata rules:** keys are `[a-z0-9-]`, values any string, at most 640 UTF-8 bytes in all; `openrag-` is kept for the adapters' own entries.
  - **Writes:** a failed `put_stream` leaves the key as it was.
  - **Not added yet:** `list`.
- **Why:**
  - S3 streaming without presigning needs the total size for `Content-Range`, and the conformance case "metadata" needs a way to read metadata back.
  - 640 bytes, tripled by the percent-encoding S3's ASCII headers need, plus the adapter's SHA entry, still fits S3's 2 KiB.
- **Rejected:**
  - write-only metadata: it can't be tested the same way on both adapters;
  - `list` now: nothing calls it before the orphan script runs on S3, so it gets added then.

## D5. An in-memory ObjectStore fake, held to the conformance suite

**#1429**, `tests/unit/conftest.py` (#1470). Status: taken.

- **Decision:** `InMemoryObjectStore` (fixture `memory_object_store`) runs the same conformance suite as the real adapters.
- **Why:**
  - it checks the suite itself before any adapter exists: seven deliberately broken fakes each failed it;
  - it gives the unit tests of the code moved onto the port a fake that behaves like a real store.
- **Rejected:** a `MagicMock` of the port, which doesn't behave like a store.

## D6. The filesystem keeps metadata in a hidden sidecar, under a per-key lock

**#1429**, `services/storage/fs_object_store.py` (#1470). Status: taken.

- **Decision:**
  - next to each object, a `.{name}.meta` JSON holds its SHA-256 and metadata;
  - the content and the sidecar are renamed in place under a per-key lock, within one process;
  - a legacy path can't have a hidden (`.name`) part, so no key reaches a sidecar or a `.part-` temporary file.
- **Why:**
  - it works on any volume, NFS included;
  - without the lock, 3 of 30 runs of the same-key race paired one write's content with the other's metadata.
- **Rejected:**
  - extended attributes: missing on EFS, Azure Files and older NFS, the shared volumes `fs` runs on in Kubernetes;
  - a lock across processes: `flock` is unreliable on NFS, and OpenRag writes each key once, one upload id per revision.
- **Also:** a put cancelled after its stream was read still completes, and the port's docstring now says so. Undoing the rename at that point would lose the previous object.

## D7. The port's PR wires the store but injects it nowhere, and adds no setting

**#1429**, `di/object_stores.py`, `ServiceContainer.upload_store` (#1470). Status: taken.

- **Decision:**
  - `create_upload_store(settings)` builds `FsObjectStore(paths.data_dir)`, and the container exposes it as `upload_store`, built once;
  - nothing reads it yet;
  - no `UPLOAD_STORE` setting.
- **Why:**
  - #1429 lists the port as a change with no behavior change. Injecting it where `data_dir` is used means changing those callers, which is its next change (upload routes, worker and `/static` through the port);
  - a setting with one possible value is dead configuration; `UPLOAD_STORE=fs|s3` comes with the S3 adapter.
- **Rejected:** a container field that is rebuilt on each read. One store per process keeps one set of per-key locks (D6).

## D8. A replace reads the previous `source` before its commit, under the fence

**1A**, `indexer_actor.process_file`. Status: taken.

- **Decision:** the worker reads the row's `source` with `get_file_metadata` before `update_file_in_partition`, and deletes it once the commit succeeds, before it releases the fence.
- **Why:** `update_file_in_partition` returns a bool that five tests assert on. Under Postgres fences (the v2.4.0 default) replaces are serialized, so the read is exact.
- **Rejected:** `UPDATE … RETURNING` (IDX_01), which changes that return type for no gain under Postgres fences.
- **Impact:** under Ray fences nothing is deleted (D11), so the leak two concurrent replaces could cause there never happens. A delete checks references first, so it never takes an original a row references at that moment; a copy still running is the exception (D12).

## D9. A failed job's upload is deleted where the job settles, unless a row references it

**1A**, `indexer_pool`. Status: taken.

- **Decision:** at settle, with `SAVE_UPLOADED_FILES=true` and an unsuccessful task, the pool deletes the upload and its `.md` unless a catalog row references it.
- **Why:**
  - pre-flight failures and a lost worker lease never enter `process_file`'s failure path (IDX_01's place);
  - a job that fails after its catalog commit leaves an indexed file, whose original must stay;
  - no row references a failed upload, so deleting it after the fence is released is safe.
- **Rejected:** deleting it in `process_file`'s failure path, which misses those failures.

## D10. Originals are deleted only under `data_dir`, after a reference check

**1A**, `services/storage/originals.py`. Status: taken.

- **Decision:** `OriginalsCleaner` deletes a path, with its `.md`, only if it resolves under `data_dir` (symlinks followed) and `referenced_sources` no longer names it. If the check fails, the whole batch stays.
- **Why:** a copy keeps its source's `source`, possibly in another partition. The resolve keeps a bad path from reaching outside the data directory.
- **Impact:** an MCP `index_url` original, which sits in the node's temp directory, is never deleted by 1A. It's kept only with `SAVE_UPLOADED_FILES=true`, and 1C moves it into the store (#1003).

## D11. Originals are deleted only with `FILE_FENCES=postgres`

**1A**, `originals_cleaner_for`, used by the API container and each indexer worker. Status: taken.

- **Decision:** a process builds the cleaner only with `FILE_FENCES=postgres`, the v2.4.0 default. Otherwise there is none: every delete, replace and failed job keeps its original, as today.
- **Why:** IDX_01 says 1A can ship in 2.3.x, but IDX_00 keeps behavior changes off until v2.4.0, and 2.3.x is cut from `develop`. A deleted file can't be brought back, so this lands with the release that announces it. Tying it to the fences adds no setting.
- **Rejected:**
  - shipping it in 2.3.x as a plain fix: `SAVE_UPLOADED_FILES` defaults to `true`, so every deployment would start deleting at once;
  - a setting of its own, dead once v2.4.0 makes it the default.
- **Impact:** 2.3.x keeps #1428. An operator who wants the fix sooner sets `FILE_FENCES=postgres`, and gets its 409s with it.

## D12. A copy running while its source is deleted can lose its original

**1A**, `WorkerDispatcher.copy_file`. Status: taken.

- **Decision:** the copy keeps running without the source file's fence, and the gap is accepted.
- **Why:**
  - the copy reads the source row, then writes its own row with the same `source` seconds later, or minutes when it re-embeds. A delete of the source in between finds no row left and deletes the original;
  - the copy stays searchable: only its download and a re-index fail;
  - a move (a copy, then a delete) is safe, since the route answers 201 only once the copy's row is written.
- **Rejected:** holding the source's fence for the whole copy. A delete or replace of the source would then wait on the copy, a change to #1459's code for a rare case.
- **Impact:** the orphan script can't bring that file back. If it happens in practice, the fence above is the fix.

## D13. The orphan script considers only uploads, and deletes nothing where the catalog knows none

**1A**, `scripts/find_orphan_originals.py`, `services/storage/orphan_originals.py`. Status: taken.

- **Decision:**
  - it lists only files directly under `data_dir` named like an upload (`make_unique_filename`), with their `.md` copies, older than `--min-age-hours` (24 by default, at least 1);
  - `--delete` fails, deleting nothing, when no upload there is referenced;
  - it isn't gated by `FILE_FENCES` (D11): an operator runs it on purpose, and listing is the default.
- **Why:** `data_dir` holds more than uploads, and the catalog stores paths as the API sees them. Run on the host, or against another database, every upload would look unreferenced.
- **Rejected:** a `--force` past that refusal. A catalog that really references no upload is rare, and the list shows what to remove by hand.

