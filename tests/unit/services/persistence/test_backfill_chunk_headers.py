"""Tests for the script adding the filename to chunks already indexed.

The fake client applies a partial upsert like Milvus: named fields change, others stay.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from core.chunking.recursive import source_header
from core.prompts.contextualization_builder import wrap_chunk_with_context
from pymilvus import MilvusException
from services.persistence.migrations.milvus import backfill_chunk_headers as backfill


def _words(text: str) -> int:
    return len(text.split())


class _FakeIterator:
    def __init__(self, rows: list[dict[str, Any]], batch_size: int) -> None:
        self._pages = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
        self.closed = False

    def next(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._pages.pop(0)] if self._pages else []

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self, rows: list[dict[str, Any]], refuse: tuple[int, ...] = (), refuse_code: int = 1100) -> None:
        self.rows = {row["_id"]: dict(row) for row in rows}
        self.refuse = set(refuse)
        self.refuse_code = refuse_code
        self.written: list[dict[str, Any]] = []
        self.filters: list[str] = []
        self.output_fields: list[list[str]] = []
        self.upsert_calls = 0
        self.flushed = 0
        self.iterators: list[_FakeIterator] = []

    def query_iterator(self, collection_name, filter, batch_size, output_fields):
        self.filters.append(filter)
        self.output_fields.append(output_fields)
        self.iterators.append(_FakeIterator(list(self.rows.values()), batch_size))
        return self.iterators[-1]

    def upsert(self, collection_name, data, partial_update):
        assert partial_update, "a full-row upsert would reassign the auto-generated _id"
        self.upsert_calls += 1
        if self.refuse & {entity["_id"] for entity in data}:
            raise MilvusException(code=self.refuse_code, message="refused")
        for entity in data:
            self.written.append(entity)
            self.rows[entity["_id"]].update(entity)

    def query(self, collection_name, filter, output_fields, limit):
        return [row for row in self.rows.values() if f'partition == "{row.get("partition")}"' == filter][:limit]

    def flush(self, collection_name):
        self.flushed += 1


def _run(client: _FakeClient, **kwargs: Any):
    return backfill.run(client, "c", length_function=_words, chunk_size=512, **kwargs)


def _row(chunk_id: int = 1, **fields: Any) -> dict[str, Any]:
    return {"_id": chunk_id, "text": "alpha beta", "file_id": "f", "filename": "report.pdf", "page": 3, **fields}


def test_adds_the_header_to_text_and_nothing_else():
    client = _FakeClient([_row(token_count=2, vector_field=[0.1], tag="kept")])

    stats = _run(client)

    assert client.written == [
        {
            "_id": 1,
            "text": "[Source: report.pdf]\n\nalpha beta",
            "header": "[Source: report.pdf]",
            "token_count": _words("[Source: report.pdf]\n\nalpha beta"),
        }
    ]
    assert stats["changed"] == 1
    assert client.flushed == 1
    assert client.iterators[0].closed
    # Without $meta, Milvus returns none of filename, header or token_count.
    assert {"text", "file_id", "$meta"} <= set(client.output_fields[0])


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(
            {"header": "[Source: report.pdf | Page: 3]", "text": "[Source: report.pdf | Page: 3]\n\nbody"},
            id="already-has-a-header",
        ),
        pytest.param(
            {"text": "[CONTEXT]\n\na context\n\n* filename: report.pdf\n\n[CHUNK_START]\n\nbody\n\n[CHUNK_END]"},
            id="contextualized-by-an-earlier-release",
        ),
        pytest.param(
            {"text": "* filename: report.pdf\n\n[CHUNK_START]\n\nbody\n\n[CHUNK_END]"},
            id="contextualization-failed-in-an-earlier-release",
        ),
        pytest.param(
            {
                "header": "[Source: report.pdf]",
                "content": "[Source: report.pdf]\n\nbody",
                "text": wrap_chunk_with_context("[Source: report.pdf]\n\nbody", "a context"),
            },
            id="contextualized",
        ),
        pytest.param(
            {
                "header": "[Source: report.pdf | Page: 3]",
                "content": "[Source: report.pdf | Page: 3]\n\nbody",
                "text": wrap_chunk_with_context("[Source: report.pdf | Page: 3]\n\nbody"),
            },
            id="contextualization-failed",
        ),
        pytest.param(
            {"content": "[Source: report.pdf]\n\nbo", "text": wrap_chunk_with_context("[Source: report.pdf]\n\nbody")},
            id="contextualized-content-cut-short",
        ),
        pytest.param({"content": "alpha beta"}, id="prepend-heading-path-off"),
        pytest.param({"content": "alpha"}, id="prepend-heading-path-off-content-cut-short"),
        pytest.param({"content": "alpha beta gamma"}, id="prepend-heading-path-off-text-cut-short"),
    ],
)
def test_leaves_chunks_that_already_name_their_file(fields):
    client = _FakeClient([_row(**fields)])

    stats = _run(client)

    assert client.written == []
    assert stats["unchanged"] == 1
    assert client.flushed == 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("The setting is called filename: see below.", id="in-prose"),
        pytest.param("Fields:\n* filename: report.pdf\n* size: 3", id="quoting-the-marker"),
    ],
)
def test_mentioning_a_filename_in_the_body_does_not_count_as_having_one(body):
    client = _FakeClient([_row(text=body)])

    assert _run(client)["changed"] == 1


def test_a_chunk_without_a_filename_is_counted_not_guessed():
    client = _FakeClient([_row(filename=None, source="/app/data/p/123_report.pdf")])

    stats = _run(client)

    assert client.written == []
    assert stats["no_filename"] == 1


@pytest.mark.parametrize(
    ("fields", "kept"),
    [
        pytest.param({"header": "Q3 report"}, {"header": "Q3 report"}, id="header"),
        pytest.param({"content": "a summary"}, {"content": "a summary", "header": None}, id="content"),
    ],
)
def test_upload_metadata_named_like_a_chunk_field_does_not_block_the_header(fields, kept):
    original = _row(token_count=2, **fields)
    client = _FakeClient([original])

    assert _run(client)["changed"] == 1
    assert client.rows[1]["text"] == "[Source: report.pdf]\n\nalpha beta"
    assert client.rows[1].get("content") == fields.get("content")
    assert client.rows[1]["header"] == fields.get("header", "[Source: report.pdf]")
    assert _run(client)["changed"] == 0

    assert _run(client, revert=True)["changed"] == 1
    assert client.rows[1]["text"] == original["text"]
    assert client.rows[1]["token_count"] == original["token_count"]
    assert {key: client.rows[1].get(key) for key in kept} == kept


def test_a_long_filename_is_shortened_as_on_new_chunks():
    name = "x" * 1000
    client = _FakeClient([_row(filename=name)])

    _run(client)

    header = client.written[0]["header"]
    assert header == source_header(name, 512, _words)
    assert len(header) < len(name)
    assert _run(client)["changed"] == 0
    assert _run(client, revert=True)["changed"] == 1
    assert client.rows[1]["text"] == "alpha beta"


def test_running_it_again_changes_nothing():
    client = _FakeClient([_row(1), _row(2)])
    _run(client)
    client.written.clear()

    stats = _run(client)

    assert client.written == []
    assert stats["unchanged"] == 2


def test_section_ids_a_partial_upsert_would_round_are_folded():
    big = 2**60 + 1
    client = _FakeClient([_row(section_id=big, next_section_id=7)])

    _run(client)

    written = client.written[0]
    assert written["section_id"] == big & (2**53 - 1)
    assert "next_section_id" not in written


def test_other_integers_a_partial_upsert_rounds_are_reported():
    client = _FakeClient([_row(external_ref=2**60 + 1)])

    with patch.object(backfill, "logger") as logger:
        _run(client)

    assert logger.bind.call_args.kwargs["rounded"] == {"f": ["external_ref"]}


def test_a_refused_chunk_does_not_cost_the_rest_of_its_batch():
    client = _FakeClient([_row(i) for i in range(1, 6)], refuse=(3,))

    stats = _run(client)

    assert sorted(entity["_id"] for entity in client.written) == [1, 2, 4, 5]
    assert stats["changed"] == 4
    assert stats["refused"] == 1


def test_an_outage_stops_the_run_instead_of_splitting_the_batch():
    client = _FakeClient([_row(i) for i in range(1, 6)], refuse=(3,), refuse_code=1)

    with pytest.raises(MilvusException):
        _run(client)

    assert client.upsert_calls == 1
    assert client.iterators[0].closed


def test_stops_when_milvus_refuses_every_chunk():
    count = backfill._MAX_REFUSED_WITH_NOTHING_WRITTEN
    client = _FakeClient([_row(i) for i in range(1, count * 3)], refuse=tuple(range(1, count * 3)))

    with pytest.raises(RuntimeError, match="refused all"):
        _run(client, batch_size=count)

    assert client.upsert_calls == 2 * count - 1


def test_refusals_do_not_stop_the_run_once_a_chunk_was_written():
    count = backfill._MAX_REFUSED_WITH_NOTHING_WRITTEN
    client = _FakeClient([_row(i) for i in range(1, count + 2)], refuse=tuple(range(2, count + 2)))

    stats = _run(client, batch_size=count + 1)

    assert stats["changed"] == 1
    assert stats["refused"] == count


def test_dry_run_writes_nothing():
    client = _FakeClient([_row()])

    stats = _run(client, dry_run=True)

    assert client.written == []
    assert client.flushed == 0
    assert stats["changed"] == 1


def test_partition_filter():
    client = _FakeClient([_row()])

    _run(client, partitions=["a", 'b"c'])
    _run(client)

    assert client.filters == ['partition in ["a", "b\\"c"]', ""]


def test_partition_filter_keeps_non_ascii_raw():
    client = _FakeClient([])

    _run(client, partitions=["team📁"])

    assert client.filters == ['partition in ["team📁"]']


def test_an_unknown_partition_stops_before_anything_is_written(monkeypatch):
    client = _FakeClient([_row(partition="known")])
    client.has_collection = lambda name: True
    monkeypatch.setattr("sys.argv", ["backfill", "--partition", "known", "--partition", "typo"])
    monkeypatch.setattr(backfill, "MilvusClient", lambda uri: client)
    monkeypatch.setattr(backfill, "get_num_tokens", lambda: _words)
    config = SimpleNamespace(
        vectordb=SimpleNamespace(host="h", port=1, collection_name="c"), chunker=SimpleNamespace(chunk_size=512)
    )
    monkeypatch.setattr(backfill, "load_config", lambda: config)

    with pytest.raises(SystemExit) as exit_info:
        backfill.main()

    assert exit_info.value.code == 1
    assert client.iterators == []
    assert client.upsert_calls == 0


def test_reads_page_by_page():
    client = _FakeClient([_row(i) for i in range(1, 6)])

    stats = _run(client, batch_size=2)

    assert stats["scanned"] == 5
    assert stats["changed"] == 5


def test_revert_restores_what_it_changed():
    original = _row(token_count=2)
    client = _FakeClient([original])
    _run(client)
    assert client.rows[1]["text"] != original["text"]

    stats = _run(client, revert=True)

    assert client.rows[1]["text"] == original["text"]
    assert client.rows[1]["token_count"] == original["token_count"]
    assert client.rows[1]["header"] is None
    assert stats["changed"] == 1

    # The cleared header is what lets a later run add it back.
    assert _run(client)["changed"] == 1


@pytest.mark.parametrize(
    ("header", "content"),
    [
        pytest.param("[Source: report.pdf | Page: 3]", "body", id="structured-section"),
        pytest.param("[Source: report.pdf]", "body", id="recursive-splitter"),
        pytest.param("[Source: report.pdf]", "bo", id="content-cut-short"),
    ],
)
def test_revert_leaves_headers_a_chunker_wrote(header, content):
    client = _FakeClient([_row(text=f"{header}\n\nbody", header=header, content=content)])

    stats = _run(client, revert=True)

    assert client.written == []
    assert stats["unchanged"] == 1


def test_revert_leaves_a_contextualized_chunk():
    body = "[Source: report.pdf]\n\nbody"
    client = _FakeClient(
        [_row(text=wrap_chunk_with_context(body, "a context"), header="[Source: report.pdf]", content=body)]
    )

    stats = _run(client, revert=True)

    assert client.written == []
    assert stats["unchanged"] == 1


def test_revert_leaves_a_document_that_starts_with_its_own_source_line():
    client = _FakeClient([_row(text="[Source: report.pdf]\n\nbody")])

    stats = _run(client, revert=True)

    assert client.written == []
    assert stats["unchanged"] == 1


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("Introduction", id="plain-text"),
        pytest.param("[Source: other.pdf]", id="source-line-naming-another-file"),
        pytest.param(5, id="not-a-string"),
    ],
)
def test_revert_leaves_an_upload_header_that_starts_the_text(header):
    client = _FakeClient([_row(text=f"{header}\n\nbody", header=header)])

    stats = _run(client, revert=True)

    assert client.written == []
    assert stats["unchanged"] == 1


def test_revert_keeps_an_upload_header_and_removes_only_the_line_it_added():
    original = _row(text="Introduction\n\nbody", header="Note")
    client = _FakeClient([original])
    _run(client)

    _run(client, revert=True)

    assert client.rows[1]["text"] == original["text"]
    assert client.rows[1]["header"] == "Note"


def test_revert_recognizes_a_line_shortened_under_another_chunk_size():
    long_name = "report " * 200 + ".pdf"
    header = source_header(long_name, 50, _words)
    assert header != source_header(long_name, 512, _words)
    client = _FakeClient([_row(text=f"{header}\n\nbody", header=header, filename=long_name)])

    _run(client, revert=True)

    assert client.rows[1]["text"] == "body"
    assert client.rows[1]["header"] is None
