# Phase 4: Async Infrastructure - Research

**Researched:** 2026-02-10
**Domain:** Python async/await patterns, file I/O in async contexts, Ray actor async patterns
**Confidence:** HIGH

## Summary

Phase 4 addresses blocking I/O operations in async contexts across two main areas: file loaders and the restore script. The codebase already demonstrates strong async patterns in some areas (VideoAudioLoader uses `asyncio.to_thread` extensively, aiofiles is used in `files.py`), but several loaders still perform blocking file operations in async methods. The restore script uses blocking `ray.get()` which prevents proper async behavior.

The standard Python async ecosystem provides two primary solutions for file I/O: `aiofiles` (dedicated async file library, already in use) and `asyncio.to_thread()` (general-purpose thread pool executor, already used extensively). For Ray actors, the pattern is clear: use `await` with Ray ObjectRefs instead of blocking `ray.get()`.

**Primary recommendation:** Use `asyncio.to_thread()` for blocking file operations in loaders (consistency with existing codebase patterns), and convert restore script functions to async with `await` for Ray actor calls.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| asyncio | stdlib (Python 3.9+) | Event loop and async primitives | Python's built-in async runtime, no additional dependency |
| asyncio.to_thread | stdlib (Python 3.9+) | Offload blocking I/O to thread pool | Modern, context-aware thread delegation with GIL release |
| aiofiles | Not currently in deps (latest: 25.1.0) | Async file operations | Already imported and used in `components/indexer/utils/files.py` but not in pyproject.toml |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| Ray async actors | ray[default]>=2.47.1 | Concurrent actor method execution | Already in use; use `await` instead of `ray.get()` in async contexts |
| concurrent.futures.ThreadPoolExecutor | stdlib | Manual thread pool control | Only if custom executor configuration needed (not required for this phase) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| asyncio.to_thread | aiofiles | aiofiles provides dedicated file API but adds dependency; to_thread is stdlib and already used extensively in codebase |
| asyncio.to_thread | loop.run_in_executor | to_thread is more modern (Python 3.9+), supports contextvar propagation, simpler API |
| Thread pool | Process pool | Process pool for CPU-bound tasks, not I/O-bound file operations |

**Installation:**
```bash
# No new dependencies required for asyncio.to_thread (stdlib)
# aiofiles MAY be needed if discovered to be missing from actual environment
uv add aiofiles  # Only if import fails in practice
```

## Architecture Patterns

### Recommended Async File I/O Pattern

**Codebase already follows this pattern in VideoAudioLoader:**

```python
# Pattern: asyncio.to_thread for blocking file operations
import asyncio
from pathlib import Path

async def aload_document(self, file_path, metadata, save_markdown=False):
    # Blocking file read → offload to thread
    content = await asyncio.to_thread(self._blocking_file_operation, file_path)

    # Blocking file write → offload to thread
    if save_markdown:
        await asyncio.to_thread(self._save_to_disk, content, file_path)

    return Document(page_content=content, metadata=metadata)
```

**Current blocking pattern (needs fixing):**

```python
# Anti-pattern: Blocking file I/O in async method
async def aload_document(self, file_path, metadata, save_markdown=False):
    # ❌ Blocks event loop
    with open(file_path, "rb") as f:
        data = f.read()

    # ❌ Blocks event loop
    with zipfile.ZipFile(file_path, "r") as zip_file:
        files = zip_file.namelist()
```

### Pattern 1: BaseLoader.save_content() Async Conversion

**Current implementation (blocking):**
```python
# Source: openrag/components/indexer/loaders/base.py:54-58
def save_content(self, text_content: str, path: str):
    path = re.sub(r"\..*", ".md", path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text_content)
    logger.debug(f"Document saved to {path}")
```

**Target pattern:**
```python
async def save_content(self, text_content: str, path: str):
    path = re.sub(r"\..*", ".md", path)
    await asyncio.to_thread(self._write_file, path, text_content)
    logger.debug(f"Document saved to {path}")

def _write_file(self, path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
```

### Pattern 2: Ray Actor Async Calls (Restore Script)

**Current blocking pattern:**
```python
# Source: openrag/scripts/restore.py:261-263
ray.get(
    vdb_tmp.__ray_ready__.remote()
)  # ensure the actor is fully initialized
```

**Target async pattern:**
```python
# Use await instead of ray.get in async function
async def main():
    vdb_tmp = MilvusDB.options(name="Vectordb", namespace="openrag", lifetime="detached").remote()
    await vdb_tmp.__ray_ready__.remote()  # Non-blocking wait
    print("VectorDB (Milvus) actor fully initialized")
```

### Pattern 3: File Reading in Loaders

**Blocking operations identified:**

1. **EmlLoader** (`eml_loader.py:33-34`): `with open(file_path, "rb") as fhdl: raw_email = fhdl.read()`
2. **DocxLoader** (`docx.py:61, 73`): `with zipfile.ZipFile(input_file, "r") as docx: image_data = docx.read(image_file)`
3. **DocLoader** (`doc.py:20-28`): `document.LoadFromFile(str(file_path))`, `document.SaveToFile(file_path, FileFormat.Docx2016)`, `os.remove(file_path)`
4. **ImageLoader** (`image.py:28-31`): `cairosvg.svg2png(url=str(path))`, `img = Image.open(path)`
5. **PyMuPDF4LLMLoader** (`pymupdf.py:35`): `pages = pymupdf4llm.to_markdown(file_path, ...)`
6. **PPTXLoader** (`pptx_loader.py:152`): `md_content, imgs = self.converter.convert(local_path=file_path)` (calls `pptx.Presentation(local_path)`)

**Target conversion:**
```python
# Wrap blocking library calls with asyncio.to_thread
async def aload_document(self, file_path, metadata, save_markdown=False):
    # Blocking: pptx.Presentation(file_path)
    md_content, imgs = await asyncio.to_thread(
        self.converter.convert,
        file_path
    )
    # ... rest of async processing
```

### Pattern 4: Tempfile Operations

**Current blocking pattern (doc.py:23-27):**
```python
with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp_file:
    file_path = temp_file.name
    document.SaveToFile(file_path, FileFormat.Docx2016)
# ...
os.remove(file_path)
```

**Target pattern:**
```python
# tempfile creation is fast, but SaveToFile and os.remove are blocking
temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
file_path = temp_file.name
temp_file.close()

await asyncio.to_thread(document.SaveToFile, file_path, FileFormat.Docx2016)
# ... processing
await asyncio.to_thread(os.remove, file_path)
```

### Anti-Patterns to Avoid

- **Don't call `ray.get()` in async functions** - Use `await` directly on Ray ObjectRefs instead
- **Don't block the event loop with file I/O** - Even "quick" file operations can block under load
- **Don't use `run_in_executor` when `to_thread` exists** - `to_thread` is cleaner and supports contextvar propagation
- **Don't make async methods that don't await** - If a method is synchronous, keep it sync; only mark async if it uses await

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Thread pool management | Custom thread pool wrapper | `asyncio.to_thread()` | Automatic thread pool sizing (32 or CPU+4), contextvar propagation, simpler API |
| Async file operations | Manual executor-based file wrapper | `asyncio.to_thread()` or `aiofiles` | Well-tested, handles edge cases (encoding, buffering, context managers) |
| Ray actor timeouts | Manual timeout logic with threading | `call_ray_actor_with_timeout()` utility | Already exists in codebase (`components/ray_utils.py`), handles cancellation properly |
| Async context managers | Manual __aenter__/__aexit__ | `async with asyncio.to_thread(context_manager)` won't work - use thread-safe patterns or aiofiles | Context managers don't compose well with to_thread; need library support |

**Key insight:** File I/O is I/O-bound and releases the GIL, making ThreadPoolExecutor ideal. CPU-bound operations need ProcessPoolExecutor, but we have none in this phase.

## Common Pitfalls

### Pitfall 1: Blocking zipfile Operations in Async Methods

**What goes wrong:** `zipfile.ZipFile` read/write operations block the event loop, preventing concurrent processing of other files.

**Why it happens:** zipfile is a synchronous library with no async alternative; developers call it directly in async methods.

**How to avoid:** Wrap all zipfile operations with `asyncio.to_thread()`:

```python
# Bad
async def get_images_from_zip(self, input_file):
    with zipfile.ZipFile(input_file, "r") as docx:  # ❌ Blocking
        file_names = docx.namelist()
        for image_file in image_files:
            image_data = docx.read(image_file)  # ❌ Blocking

# Good
async def get_images_from_zip(self, input_file):
    file_names, image_data_list = await asyncio.to_thread(
        self._extract_images_from_zip, input_file
    )

def _extract_images_from_zip(self, input_file):
    # Runs in thread pool
    with zipfile.ZipFile(input_file, "r") as docx:
        file_names = docx.namelist()
        # ... extract all data
    return file_names, image_data_list
```

**Warning signs:** `with zipfile.ZipFile` in an async function, high latency when processing multiple DOCX/PPTX files concurrently.

### Pitfall 2: Using ray.get() in Async Functions

**What goes wrong:** `ray.get()` blocks the entire event loop thread, preventing all async operations from progressing.

**Why it happens:** Ray's sync API (`ray.get()`) is more familiar than async patterns; developers use it in async functions without realizing the blocking behavior.

**How to avoid:** Always use `await` directly on Ray ObjectRefs in async contexts:

```python
# Bad
async def main():
    actor = SomeActor.remote()
    result = ray.get(actor.method.remote())  # ❌ Blocks event loop

# Good
async def main():
    actor = SomeActor.remote()
    result = await actor.method.remote()  # ✓ Non-blocking
```

**Warning signs:** Documented in Ray official docs - "Running blocking ray.get or ray.wait inside async actor method is not allowed, because ray.get will block the execution of the event loop."

### Pitfall 3: Converting Sync Methods to Async Without Awaiting

**What goes wrong:** Changing `def` to `async def` without adding `await` creates a "fake async" method that still blocks.

**Why it happens:** Developers think adding `async` keyword is sufficient; they don't realize the method still executes synchronously.

**How to avoid:** Only convert methods to async if they will use `await`; otherwise keep them synchronous and call with `asyncio.to_thread()`:

```python
# Bad - "fake async"
async def save_content(self, text_content: str, path: str):
    with open(path, "w") as f:  # ❌ Still blocking, async keyword does nothing
        f.write(text_content)

# Good - actually async
async def save_content(self, text_content: str, path: str):
    await asyncio.to_thread(self._write_file, path, text_content)  # ✓ Non-blocking

def _write_file(self, path: str, content: str):
    with open(path, "w") as f:
        f.write(content)
```

**Warning signs:** Async function with no `await` statements, PyCharm warning "This function could be made non-async".

### Pitfall 4: Not Propagating Async Changes to Callers

**What goes wrong:** Converting `save_content()` from sync to async breaks all existing callers unless they also add `await`.

**Why it happens:** Changing method signatures has cascading effects; easy to miss call sites.

**How to avoid:** Search for all call sites before converting; update them in the same change:

```python
# Before: sync method
def save_content(self, text_content: str, path: str):
    with open(path, "w") as f:
        f.write(text_content)

# After: async method
async def save_content(self, text_content: str, path: str):
    await asyncio.to_thread(self._write_file, path, text_content)

# Update ALL callers in loaders
async def aload_document(self, file_path, metadata, save_markdown=False):
    # ...
    if save_markdown:
        await self.save_content(content, str(file_path))  # Add await
```

**Warning signs:** grep for `self.save_content\(` and `super().save_content\(` to find all call sites.

### Pitfall 5: Mixing Async and Blocking Methods in Ray Actors

**What goes wrong:** Ray actors with both async and regular methods can exhibit unexpected concurrency behavior even with `max_concurrency` configured.

**Why it happens:** Ray's async actor scheduling treats async and sync methods differently; mixing them creates race conditions.

**How to avoid:** Keep Ray actors either fully async or fully sync; don't mix:

```python
# Bad - mixing async and sync methods
@ray.remote
class MixedActor:
    async def async_method(self):  # ❌ Mixed with sync method
        await asyncio.sleep(1)

    def sync_method(self):  # ❌ Mixed with async method
        time.sleep(1)

# Good - split into separate actors if needed
@ray.remote
class AsyncActor:
    async def method(self):
        await asyncio.sleep(1)

@ray.remote(max_concurrency=10)
class ThreadedActor:
    def method(self):
        time.sleep(1)
```

**Warning signs:** Actor has both `async def` and `def` methods; documented in Ray GitHub issue #49869.

## Code Examples

Verified patterns from official sources:

### asyncio.to_thread Usage (Existing Codebase)

```python
# Source: openrag/components/indexer/loaders/media_loader.py:35
sound = await asyncio.to_thread(AudioSegment.from_wav, wav_path)

# Source: openrag/components/indexer/loaders/media_loader.py:57
await asyncio.to_thread(segment.export, tmp_path, format="wav")

# Source: openrag/components/indexer/loaders/media_loader.py:222
sound = await asyncio.to_thread(AudioSegment.from_file, file=path, format=path.suffix[1:])
```

### Ray Actor Async Pattern (Existing Codebase)

```python
# Source: openrag/components/ray_utils.py:11-58
async def call_ray_actor_with_timeout(
    future: ray.ObjectRef,
    timeout: float,
    task_description: str = "Ray task",
) -> Any:
    """Wait for a Ray actor call with timeout and proper cancellation handling."""
    try:
        result = await asyncio.wait_for(asyncio.gather(future), timeout=timeout)
        return result[0]
    except TimeoutError:
        logger.warning(f"{task_description} timed out, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise
    # ... exception handling
```

### Blocking Pattern - What NOT to Do

```python
# Source: openrag/components/indexer/loaders/base.py:54-58
# ❌ Anti-pattern: Blocking file write in BaseLoader
def save_content(self, text_content: str, path: str):
    path = re.sub(r"\..*", ".md", path)
    with open(path, "w", encoding="utf-8") as f:  # Blocks event loop
        f.write(text_content)
    logger.debug(f"Document saved to {path}")
```

### Converting PIL Image Operations

```python
# Source: openrag/components/indexer/loaders/image.py:28-31
# ❌ Current blocking pattern
if path.suffix.lower() == ".svg":
    png_data = cairosvg.svg2png(url=str(path))  # Blocks
    img = Image.open(BytesIO(png_data))
else:
    img = Image.open(path)  # Blocks

# ✓ Target async pattern
if path.suffix.lower() == ".svg":
    png_data = await asyncio.to_thread(cairosvg.svg2png, url=str(path))
    img = await asyncio.to_thread(Image.open, BytesIO(png_data))
else:
    img = await asyncio.to_thread(Image.open, path)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `loop.run_in_executor(None, func)` | `asyncio.to_thread(func)` | Python 3.9 (2020) | Simpler API, contextvar propagation, type hints |
| `ray.get()` in all contexts | `await` in async contexts | Ray 1.0+ (2020) | Non-blocking actor calls, proper async/await semantics |
| Manual thread pool sizing | Default thread pool (32 or CPU+4) | Python 3.8+ | Automatic resource management, no configuration needed |
| aiofiles for all file I/O | `asyncio.to_thread()` for simple operations | Python 3.9+ | Fewer dependencies for basic operations; aiofiles still preferred for complex file handling |

**Deprecated/outdated:**
- Using `loop.run_in_executor()` directly when `asyncio.to_thread()` exists (Python 3.9+)
- Calling `ray.get()` in async functions (documented as "not allowed" in Ray async actor docs)
- Creating custom thread pool executors for basic file I/O (stdlib thread pool is sufficient)

## Open Questions

1. **Is aiofiles actually installed in the production environment?**
   - What we know: `aiofiles` is imported in `components/indexer/utils/files.py` but NOT listed in `pyproject.toml` dependencies
   - What's unclear: How does the code currently work without it in pyproject.toml? Is it a transitive dependency?
   - Recommendation: Check if aiofiles is a transitive dep; if not, add to pyproject.toml to make dependency explicit

2. **Should restore.py be converted to async or run in a separate sync wrapper?**
   - What we know: restore.py is a standalone CLI script with blocking `ray.get()` call; main() is currently synchronous
   - What's unclear: Can we make main() async and use `asyncio.run()`, or does Ray initialization require sync context?
   - Recommendation: Test making main() async with `asyncio.run(main())`; if Ray actors work with await, proceed; otherwise use sync wrapper with `loop.run_until_complete()`

3. **What is the test coverage for file loader error handling?**
   - What we know: 93 tests exist; file operations currently synchronous
   - What's unclear: Do tests mock file operations? Will async changes break mocks?
   - Recommendation: Run tests after each loader conversion; check if `unittest.mock` patches need `AsyncMock` for async methods

4. **Are there performance benchmarks for concurrent file loading?**
   - What we know: Blocking I/O prevents concurrent file processing
   - What's unclear: What is current vs expected throughput improvement?
   - Recommendation: Consider adding a benchmark test that measures concurrent file loading before/after async changes

## Sources

### Primary (HIGH confidence)
- Python asyncio documentation - https://docs.python.org/3/library/asyncio.html
- Ray AsyncIO for Actors documentation - https://docs.ray.io/en/latest/ray-core/actors/async_api.html
- Codebase: `openrag/components/indexer/loaders/*` - actual file loader implementations
- Codebase: `openrag/scripts/restore.py` - blocking ray.get() usage
- Codebase: `openrag/components/ray_utils.py` - existing async Ray patterns

### Secondary (MEDIUM confidence)
- [High-Performance Python: AsyncIO vs Multiprocessing vs ThreadPools (2026 Guide)](https://medium.com/@yogeshkrishnanseeniraj/high-performance-python-asyncio-vs-multiprocessing-vs-threadpools-2026-guide-ad49d40452fc)
- [How to Use aiofiles for Async File Operations](https://oneuptime.com/blog/post/2026-02-03-python-aiofiles-async-files/view)
- [Pattern: Using asyncio to run actor methods concurrently — Ray](https://docs.ray.io/en/latest/ray-core/patterns/concurrent-operations-async-actor.html)
- [Event Loop — Python 3.14.3 documentation](https://docs.python.org/3/library/asyncio-eventloop.html)
- [aiofiles · PyPI](https://pypi.org/project/aiofiles/)

### Tertiary (LOW confidence)
- Web search results suggesting aiofiles 25.1.0 is latest version (not verified with official PyPI API)
- Medium articles discussing async patterns (general guidance, not authoritative)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - `asyncio.to_thread()` is stdlib and already used in codebase; Ray async patterns documented
- Architecture: HIGH - Existing codebase shows clear examples (VideoAudioLoader, ray_utils.py); Ray docs are authoritative
- Pitfalls: HIGH - Ray blocking behavior documented officially; zipfile/PIL blocking is verifiable from library source; cascading caller updates is standard refactoring risk

**Research date:** 2026-02-10
**Valid until:** 2026-04-10 (60 days - stdlib APIs and Ray patterns are stable; aiofiles version may update but API is stable)
