# Decisions

Choices the plan (IDX_00 to IDX_09) left open or that depart from it. One file for every step.

**Adding one:** append `D<n>` and never renumber. Keep it to a few lines: decision, why, what was rejected, impact.

**Status:**

- `taken`: done, not yet confirmed;
- `confirmed`: say by whom;
- `reversed`: link the decision that replaces it.

---

## D1. The parse suffix is part of the key

**1B**, `object_keys.py`. Status: taken.

- **Decision:** keys end with `{upload_id}{suffix}` (`…/0123…cdef.pdf`). The suffix is checked as a dot plus 1 to 16 letters or digits. The filename stays out of the key.
- **Why:** the filesystem `local_copy` must yield the stored file itself *and* keep the parse suffix (IDX_01). Both hold only if the stored path ends with the suffix.
- **Rejected:** symlinking or copying to a suffixed path on every parse.

## D2. A partition or file id over 191 bytes is refused (400)

**1B**, `object_keys.py`. Takes effect in 1C. Status: taken.

- **Decision:** 191 bytes encode to 255, the limit for one filesystem path component.
- **Why:** the key stays reversible and simple.
- **Rejected:**
  - hashing a component that is too long (the key no longer gives the id back);
  - splitting it over several segments (harder to parse).
- **Impact:** a new refusal, since nothing limits these ids today; it goes in the v2.4.0 notes. Twake isn't affected: it sends the instance domain and a 32-hex CouchDB id (cozy-stack fe2912c6f, `model/rag/index.go`).

## D3. With `fs`, new uploads use the key layout and older files stay in place

**1B** (`legacy_key`, the filesystem adapter); shapes 1C. Status: taken, to confirm.

- **Decision:**
  - new files go to `data_dir/<key>`;
  - files uploaded before stay where they are, and the adapter maps `legacy/<path>` to `data_dir/<path>` with nothing copied;
  - `originals`, `uploads`, `tmp` and `legacy` are reserved at the top of `data_dir`.
- **Why:** we read IDX_01's "same files in the same places" as the same volume, not the same layout. It also means no migration on `fs`.
- **Impact (checked on develop c7643617a):** nothing breaks. Parsers, sidecars, cleanup and type detection use the path or its suffix. Displayed names, Chainlit and Twake read `filename`, never `source`. No existing upload uses a reserved name. **1C must** send the catalog `filename` as the `/static` "Save as" name, in the same PR that writes the new layout: `download.py:95` uses the path's name, which would become `<uuid>.pdf`. Rolling back past 1C is safe, because a new `source` is still a valid path under `data_dir`.
- **To confirm:** the layout is live under the defaults from 1C on, while IDX_00 says refactor PRs are inactive with the defaults. Users see no change, but operators see files in new places.

## D4. The port adds `stat` and metadata rules to IDX_01's methods

**1B**, `core/ports/object_store.py`. Status: taken.

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

**1B**, `tests/unit/conftest.py`. Status: taken.

- **Decision:** `InMemoryObjectStore` (fixture `memory_object_store`) runs the same conformance suite as the real adapters.
- **Why:**
  - it checks the suite itself before any adapter exists: seven deliberately broken fakes each failed it;
  - it gives 1C's unit tests a fake that behaves like a real store.
- **Rejected:** a `MagicMock` of the port, which doesn't behave like a store.

## D6. The filesystem keeps metadata in a hidden sidecar, under a per-key lock

**1B**, `services/storage/fs_object_store.py`. Status: taken.

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

## D7. 1B wires the store but injects it nowhere, and adds no setting

**1B**, `di/object_stores.py`, `ServiceContainer.upload_store`. Status: taken.

- **Decision:**
  - `create_upload_store(settings)` builds `FsObjectStore(paths.data_dir)`, and the container exposes it as `upload_store`, built once;
  - nothing reads it yet;
  - no `UPLOAD_STORE` setting.
- **Why:**
  - IDX_01 says both "injects it where `data_dir` is used" and "nothing uses the port yet". Injecting it means changing those callers, which is 1C;
  - a setting with one possible value is dead configuration; `UPLOAD_STORE=fs|s3` comes with the S3 adapter (1D).
- **Rejected:** a container field that is rebuilt on each read. One store per process keeps one set of per-key locks (D6).
