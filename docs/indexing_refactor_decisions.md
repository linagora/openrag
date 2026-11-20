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
