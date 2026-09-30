"""Exercise the real CSV parse/chunk/embed/store path without external services."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.chunking.structured_section import StructuredSectionChunker
from core.config.indexation import LoaderConfig
from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.indexing.topic_tags import TopicTagger
from core.models.document import Document, DocumentType
from services.workers.indexer_actor import _load_document
from services.workers.parsers.parser_dispatcher import ParserDispatcher
from services.workers.pipeline_builder import IndexingPipeline, PipelineTimeouts
from services.workers.stages.store import INDEXING_TASK_ID_METADATA_KEY


class RecordingParser(CsvParser):
    def __init__(self, events):
        super().__init__(batch_size=2)
        self.events = events
        self.closed = False

    async def parse(self, document):
        raise AssertionError("CSV indexing must consume batches, not collect the whole file")

    def iter_batches(self, document):
        try:
            for batch in super().iter_batches(document):
                self.events.append("parse")
                yield batch
        finally:
            self.closed = True


class FakeEmbedder:
    dimension = 2

    def __init__(self, events):
        self.events = events

    async def embed(self, texts):
        self.events.append("embed")
        return [[1.0, 0.0] for _ in texts]


class FakeStore:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.deleted_filters = []
        self.deleted_ids = []
        self.lookups = 0
        self.fail_at = None

    async def ensure_collection(self, *args, **kwargs):
        pass

    async def upsert(self, chunks, *, indexed_at, vector_field=None):
        self.events.append("store")
        # Only tests retain chunks so we can inspect them after the run.
        self.calls.append((list(chunks), indexed_at))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("store unavailable")
        return len(chunks)

    async def collection_exists(self, collection):
        return True

    async def query_ids_by_filter(self, collection, filters):
        self.lookups += 1
        return ["old-chunk"]

    async def delete(self, ids, collection):
        self.deleted_ids.extend(ids)
        return len(ids)

    async def delete_by_filter(self, filters):
        self.deleted_filters.append(filters)
        return 1


@pytest.fixture
def setup_pipeline():
    events = []
    parser = RecordingParser(events)
    store = FakeStore(events)
    chunker = StructuredSectionChunker(
        chunk_size=50,
        min_tokens=0,
        max_tokens=100,
        inline_threshold=0,
        length_function=lambda text: len(text.split()),
    )
    pipeline = IndexingPipeline(parser, chunker, FakeEmbedder(events), store)
    document = Document(
        id="csv-file",
        filename="people.csv",
        content_type=DocumentType.CSV,
        partition="customers",
        text="id,name\n1,Alice\n2,Bob\n3,Cam\n4,Dan\n5,Eve\n",
        metadata={"source": "people.csv"},
    )
    row = {"document": document, "partition": "customers", "task_id": "current-attempt"}
    return pipeline, row, events


async def test_batches_are_stored_before_reading_the_next_batch(setup_pipeline):
    pipeline, row, events = setup_pipeline
    result = await pipeline.run(row)
    assert events == ["parse", "embed", "store"] * 3
    assert result is row
    assert result["stage"] == "stored"
    assert result["batch_count"] == 3
    chunks = [chunk for group, _ in pipeline.vector_store.calls for chunk in group]
    assert result["stored_count"] == result["chunk_count"] == len(chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert len({timestamp for _, timestamp in pipeline.vector_store.calls}) == 1
    assert row["indexed_at"] == pipeline.vector_store.calls[0][1]
    for batch_number, (group, _) in enumerate(pipeline.vector_store.calls, 1):
        for chunk in group:
            assert chunk.document_id == "csv-file"
            assert chunk.partition == "customers"
            assert chunk.metadata["source"] == "people.csv"
            assert chunk.metadata["csv_batch_index"] == batch_number
            assert chunk.metadata["csv_columns"] == ["id", "name"]
            assert chunk.metadata["csv_row_start"] == (batch_number - 1) * 2 + 2
            assert chunk.metadata["csv_row_end"] == min(batch_number * 2 + 1, 6)
            assert chunk.metadata[INDEXING_TASK_ID_METADATA_KEY] == "current-attempt"
            assert chunk.embedding == [1.0, 0.0]
    for number, name in enumerate(["Alice", "Bob", "Cam", "Dan", "Eve"], 1):
        assert any(f"| {number} | {name} |" in chunk.text for chunk in chunks)
    assert "chunks" not in row and "processed_document" not in row
    assert pipeline.parser.closed


async def test_long_csv_cell_is_stored_as_labelled_continuations(setup_pipeline):
    pipeline, row, _ = setup_pipeline
    note = " ".join(f"word{number}" for number in range(160))
    row["document"].text = f"id,name,note\n024,Theo,{note}\n"

    result = await pipeline.run(row)

    chunks = [chunk for group, _ in pipeline.vector_store.calls for chunk in group]
    assert result["stored_count"] == len(chunks) > 1
    recovered_parts = []
    for number, chunk in enumerate(chunks, start=1):
        assert chunk.metadata["source"] == "people.csv"
        assert chunk.metadata["csv_row_start"] == chunk.metadata["csv_row_end"] == 2
        assert chunk.metadata["csv_row_number"] == 2
        assert chunk.metadata["csv_column"] == "note"
        assert chunk.metadata["csv_part"] == number
        assert chunk.metadata["csv_parts_total"] == len(chunks)
        assert "| id | name | note |" in chunk.text
        assert "| 024 | Theo |" in chunk.text
        recovered_parts.append(chunk.text.split("] ", 1)[1].rsplit(" |", 1)[0])
    assert "".join(recovered_parts) == note


@pytest.mark.parametrize("defer", [False, True])
async def test_replacement_snapshots_once_and_keeps_old_ids_until_success(setup_pipeline, defer):
    pipeline, row, _ = setup_pipeline
    pipeline = replace(pipeline, defer_replace_cleanup=defer)
    row["replace"] = True
    await pipeline.run(row)
    store = pipeline.vector_store
    assert store.lookups == 1
    assert len(store.calls) == 3
    assert store.deleted_filters == []
    if defer:
        assert row["_replace_old_chunk_ids"] == ["old-chunk"]
        assert store.deleted_ids == []
    else:
        assert store.deleted_ids == ["old-chunk"]


@pytest.mark.parametrize("failure", ["parse", "chunk", "embed", "store"])
async def test_later_failure_cleans_only_current_attempt(setup_pipeline, failure):
    pipeline, row, _ = setup_pipeline
    row["replace"] = True
    if failure == "parse":
        row["document"].text = "id,name\n1,Alice\n2,Bob\n3,extra,cell"
        error, message = ValueError, "Record 4"
    elif failure == "store":
        pipeline.vector_store.fail_at = 2
        error, message = RuntimeError, "store unavailable"
    elif failure == "embed":
        original = pipeline.embedder.embed

        async def fail_second_embed(texts):
            if pipeline.vector_store.calls:
                raise RuntimeError("embed unavailable")
            return await original(texts)

        pipeline.embedder.embed = fail_second_embed
        error, message = RuntimeError, "embed unavailable"
    else:
        original = pipeline.chunker.chunk

        def fail_second_chunk(document, partition):
            if pipeline.vector_store.calls:
                raise RuntimeError("chunk unavailable")
            return original(document, partition=partition)

        pipeline.chunker.chunk = fail_second_chunk
        error, message = RuntimeError, "chunk unavailable"
    with pytest.raises(error, match=message):
        await pipeline.run(row)
    store = pipeline.vector_store
    assert store.deleted_ids == []
    assert store.deleted_filters == [
        {
            "partition": "customers",
            "file_id": "csv-file",
            INDEXING_TASK_ID_METADATA_KEY: "current-attempt",
        }
    ]
    assert "_replace_old_chunk_ids" not in row
    assert "chunks" not in row and "processed_document" not in row
    assert pipeline.parser.closed


async def test_empty_csv_does_not_delete_existing_index(setup_pipeline):
    pipeline, row, _ = setup_pipeline
    row["replace"] = True
    row["document"].text = ""
    await pipeline.run(row)
    assert row["stored_count"] == row["batch_count"] == row["chunk_count"] == 0
    assert pipeline.vector_store.calls == pipeline.vector_store.deleted_ids == []


@pytest.mark.parametrize("wrapped", [False, True])
async def test_dispatcher_uses_csv_settings_even_with_pdf_preset(setup_pipeline, wrapped):
    pipeline, row, _ = setup_pipeline
    config = SimpleNamespace(loader=LoaderConfig(csv_batch_size=2, csv_delimiter=";"))
    dispatcher = ParserDispatcher(config)
    parser = dispatcher.for_pdf_strategy("pymupdf") if wrapped else dispatcher
    pipeline = replace(pipeline, parser=parser)
    row["document"].text = row["document"].text.replace(",", ";")
    await pipeline.run(row)
    assert row["batch_count"] == 3
    assert "| 1 | Alice |" in pipeline.vector_store.calls[0][0][0].text


async def test_worker_keeps_csv_on_disk(tmp_path, monkeypatch):
    path = tmp_path / "upload-without-extension"
    path.write_text("id,name\n1,Alice\n")

    def reject_full_read(*args, **kwargs):
        raise AssertionError("CSV must not be read into raw_bytes")

    monkeypatch.setattr(type(path), "read_bytes", reject_full_read)
    document = await _load_document(str(path), {"file_id": "csv-file", "filename": "people.csv"}, "customers")
    assert document.content_type is DocumentType.CSV
    assert document.raw_bytes is None
    assert document.source_path == str(path)
    assert LoaderConfig().file_loaders.csv == "CsvParser"
    assert LoaderConfig().mimetypes.to_dict()["text/csv"] == ".csv"


async def test_contextualization_is_rejected_before_any_write(setup_pipeline):
    pipeline, row, events = setup_pipeline
    pipeline = replace(pipeline, contextualizer=object())
    with pytest.raises(ValueError, match="disabled"):
        await pipeline.run(row)
    assert events == []


async def test_csv_tags_once_after_all_batches_with_preset_and_prompt(setup_pipeline):
    pipeline, row, events = setup_pipeline

    async def tag_after_storing(messages):
        assert len(pipeline.vector_store.calls) == 3
        return '["Customers", "Contacts", "Other"]'

    llm = SimpleNamespace(chat=AsyncMock(side_effect=tag_after_storing))
    tagger = TopicTagger(llm, "default prompt")
    pipeline = replace(pipeline, topic_tagger=tagger)
    row.update(
        filename="people.csv",
        language="fr",
        topic_tagger_prompt="custom prompt",
        indexation_config={"enable_topic_tagging": True, "max_topic_tags": 2},
    )
    await pipeline.run(row)
    llm.chat.assert_awaited_once()
    messages = llm.chat.call_args.args[0]
    assert messages[0]["content"] == "custom prompt"
    assert "Language: fr" in messages[1]["content"]
    assert "people.csv" in messages[1]["content"]
    assert "Alice" in messages[1]["content"] and "Eve" in messages[1]["content"]
    assert row["topic_tags"] == ["Customers", "Contacts"]
    assert events == ["parse", "embed", "store"] * 3
    assert row["stage"] == "stored"
    assert "chunks" not in row


@pytest.mark.parametrize("bad_csv", [True, False])
async def test_csv_does_not_tag_failed_or_empty_file(setup_pipeline, bad_csv):
    pipeline, row, _ = setup_pipeline
    tagger = SimpleNamespace(tag=AsyncMock(return_value=[]))
    pipeline = replace(pipeline, topic_tagger=tagger)
    if bad_csv:
        row["document"].text = "id,name\n1,Alice\n2,Bob\n3,extra,cell"
        with pytest.raises(ValueError, match="Record 4"):
            await pipeline.run(row)
        tagger.tag.assert_not_awaited()
    else:
        # The real tagger returns immediately without calling the LLM for no chunks.
        llm = SimpleNamespace(chat=AsyncMock())
        pipeline = replace(pipeline, topic_tagger=TopicTagger(llm, "topics"))
        row["document"].text = ""
        await pipeline.run(row)
        llm.chat.assert_not_awaited()
        assert row["topic_tags"] == []


@pytest.mark.parametrize("failure", ["bad_response", "stage_error", "timeout"])
async def test_csv_tag_failure_preserves_successful_indexing(setup_pipeline, failure):
    pipeline, row, _ = setup_pipeline
    if failure == "bad_response":
        llm = SimpleNamespace(chat=AsyncMock(return_value="not JSON"))
        tagger = TopicTagger(llm, "topics")
    else:
        tagger = SimpleNamespace(tag=AsyncMock(side_effect=RuntimeError("LLM unavailable")))
    pipeline = replace(pipeline, topic_tagger=tagger)
    if failure == "timeout":
        pipeline = replace(pipeline, timeouts=PipelineTimeouts(topic_tag=0))
    await pipeline.run(row)
    assert row["stage"] == "stored"
    assert row["stored_count"] > 0
    assert row["topic_tags"] == []
    assert "topic_tag" in row["degraded_stages"]
    assert pipeline.vector_store.deleted_filters == []


async def test_disabled_csv_tagging_never_calls_tagger(setup_pipeline):
    pipeline, row, _ = setup_pipeline
    tagger = SimpleNamespace(tag=AsyncMock())
    pipeline = replace(pipeline, topic_tagger=tagger)
    row["indexation_config"] = {"enable_topic_tagging": False}
    await pipeline.run(row)
    tagger.tag.assert_not_awaited()


async def test_cancellation_during_tagging_cleans_stored_batches(setup_pipeline):
    pipeline, row, _ = setup_pipeline
    started = asyncio.Event()

    async def wait_forever(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    pipeline = replace(pipeline, topic_tagger=SimpleNamespace(tag=wait_forever))
    task = asyncio.create_task(pipeline.run(row))
    await asyncio.wait_for(started.wait(), 5)
    assert len(pipeline.vector_store.calls) == 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(pipeline.vector_store.deleted_filters) == 1
    assert "topic_tags" not in row and "chunks" not in row


async def test_cancel_during_read_waits_for_thread_and_closes_generator(setup_pipeline):
    pipeline, row, _ = setup_pipeline
    started, release = threading.Event(), threading.Event()
    original = pipeline.parser.iter_batches

    def blocking_batches(document):
        started.set()
        release.wait(timeout=5)
        yield from original(document)

    pipeline.parser.iter_batches = blocking_batches
    task = asyncio.create_task(pipeline.run(row))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pipeline.parser.closed
    assert pipeline.vector_store.calls == []


async def test_exhausted_parse_budget_stops_before_reading(setup_pipeline):
    pipeline, row, events = setup_pipeline
    pipeline = replace(pipeline, timeouts=PipelineTimeouts(parse=-1))
    with pytest.raises(TimeoutError, match="parse time budget"):
        await pipeline.run(row)
    assert events == []
