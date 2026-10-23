"""
CSV parser checks:
    01 Basic output, identity, UTF-8 BOM and leading zeros
    02 Quoted commas, escaped quotes and multiline values
    03 Duplicate headers and empty cells
    04 Markdown escaping
    05 Reading a file without changing it
    06 Empty input
    07 Skipping the wrong number of cells
    08 Skipping an unclosed quote
    09 Allow for an explicit delimiter, ex : CsvParser(delimiter=";")
    10 Malformed records do not discard valid records
    11 Reading many records from a file without never losing or reordering them
    12 Batch sizes, repeated headers, row order and final partial batches
    13 Lazy parsing: a later bad record does not prevent reading the first batch
    14 Input stream closes after completion, an error or an early stop
    15 Reject invalid batch sizes
    16 The async parse interface collects batches and preserves document metadata
    ...

Tests call the parser directly, not the upload/dispatcher or chunking pipeline.
"""

import csv
import sys
from contextlib import closing
from types import ModuleType

import pytest
from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document
from core.utils.exceptions import ConfigError, NoIndexableContentError

# we apply the asyncio marker to every async test in this file
pytestmark = pytest.mark.asyncio


async def test_01_normal_csv():
    document = Document(
        id="people-file",
        raw_bytes="id,name\n001,Ela\n002,Ali".encode("utf-8-sig"),
        metadata={"source": "example"},
    )

    result = await CsvParser().parse(document)
    assert result.document_id == "people-file"
    assert result.metadata == {"source": "example"}
    assert len(result.text_blocks) == 1
    assert result.text_blocks[0].block_type == "table"
    assert result.text_blocks[0].text == ("| id | name |\n| --- | --- |\n| 001 | Ela |\n| 002 | Ali |")


async def test_02_quoted_and_multiline_value():
    document = Document(text='id,note\n1,"Doe, John said ""hello""\nNext line"')
    result = await CsvParser().parse(document)
    assert result.text_blocks[0].text == ('| id | note |\n| --- | --- |\n| 1 | Doe, John said "hello"<br>Next line |')


async def test_03_duplicate_headers_and_empty_cells():
    document = Document(text="name,name,note\nAli,,\n,Bob,hello")
    result = await CsvParser().parse(document)
    assert result.text_blocks[0].text == (
        "| name | name | note |\n| --- | --- | --- |\n| Ali |  |  |\n|  | Bob | hello |"
    )


async def test_04_markdown_escaping():
    document = Document(text="id,note\n1,A|B & <tag>")
    result = await CsvParser().parse(document)
    assert result.text_blocks[0].text == ("| id | note |\n| --- | --- |\n| 1 | A&#124;B &amp; &lt;tag&gt; |")


async def test_05_reading_file_preserves_original(tmp_path):
    # pytest gives this test its own temporary directory.
    path = tmp_path / "people.csv"
    original = b"id,name\n001,Ali"
    path.write_bytes(original)
    result = await CsvParser().parse(Document(source_path=str(path)))
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 001 | Ali |"
    assert path.read_bytes() == original


@pytest.mark.parametrize("encoding", ["cp1252", "latin-1"])
async def test_csv_decodes_non_utf8_sources(encoding, tmp_path):
    text = "id,name\n1,André\n2,Zoë"
    path = tmp_path / f"people-{encoding}.csv"
    path.write_bytes(text.encode(encoding))

    from_path = await CsvParser().parse(Document(source_path=str(path)))
    from_bytes = await CsvParser().parse(Document(raw_bytes=text.encode(encoding)))

    assert from_path.text_blocks[0].text == from_bytes.text_blocks[0].text
    assert "André" in from_path.text_blocks[0].text


async def test_csv_path_sample_can_end_inside_a_utf8_character(tmp_path):
    sample_size = 64 * 1024
    prefix = b"id,note\n1,"
    path = tmp_path / "boundary.csv"
    path.write_bytes(prefix + b"x" * (sample_size - len(prefix) - 1) + "é".encode() + b"\n")

    result = await CsvParser().parse(Document(source_path=str(path)))

    assert result.text_blocks[0].text.endswith("é |")


async def test_06_empty_file():
    result = await CsvParser().parse(Document(raw_bytes=b""))
    assert result.text_blocks == []


async def test_07_inconsistent_column_count():
    document = Document(text="id,name\n1,Ali,extra")

    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(document)

    assert exc_info.value.extra["skipped_rows"] == 1


async def test_08_unclosed_quote():
    document = Document(text='id,name\n1,"Ali')

    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(document)

    assert exc_info.value.extra["skipped_rows"] == 1


async def test_09_feature_explicit_delimiter():
    """Accept a delimiter option, keeping comma as the default."""
    document = Document(text='id;name\n001;"Doe; John"')
    result = await CsvParser(delimiter=";").parse(document)
    # the quoted semicolon belongs to the name and not to a new column
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 001 | Doe; John |"


async def test_delimiter_is_sniffed_when_not_explicit():
    document = Document(text='id;amount;note\n1;3,5;"valid; value"')

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == "| id | amount | note |\n| --- | --- | --- |\n| 1 | 3,5 | valid; value |"


async def test_semicolon_delimiter_is_sniffed_with_trailing_empty_cells():
    document = Document(text="a;b\n1;2;\n3;4;\n")

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    assert result.text_blocks[0].metadata["csv_columns"] == ["a", "b"]


async def test_10_feature_error_identifies_record():
    document = Document(text='id,note\n1,"first\nsecond"\n2,extra,cell')
    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text.endswith("| 1 | first<br>second |")
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 1


async def test_11_many_records_from_file(tmp_path):
    """Checking that every record in a larger CSV survives the parsing."""
    # create a csv on disk by writing records one at a time
    path = tmp_path / "many_records.csv"
    row_count = 10_000

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "name"])
        for number in range(row_count):
            writer.writerow([f"{number:06d}", f"Person {number}"])

    # give the parser a file path, and not preloaded text or bytes
    document = Document(source_path=str(path))
    result = await CsvParser().parse(document)

    # one complete Markdown table is returned
    assert len(result.text_blocks) == 1
    block = result.text_blocks[0]
    assert block.block_type == "table"
    lines = block.text.splitlines()
    assert lines[:2] == ["| id | name |", "| --- | --- |"]
    assert len(lines) == row_count + 2

    # check all records including their order and leading zeros
    for number, line in enumerate(lines[2:]):
        assert line == f"| {number:06d} | Person {number} |"


@pytest.mark.parametrize(
    ("row_count", "batch_size", "expected_sizes"),
    [(1, 2, [1]), (2, 2, [2]), (3, 2, [2, 1]), (4, 2, [2, 2]), (5, 2, [2, 2, 1]), (5, 3, [3, 2])],
)
async def test_12_batch_boundaries(row_count, batch_size, expected_sizes):
    text = "id,name\n" + "".join(f"{number:03d},Person {number}\n" for number in range(row_count))
    # collecting these tiny examples is convenient for assertions but not for large-file consumers
    batches = list(CsvParser(batch_size=batch_size).iter_batches(Document(text=text)))
    sizes = []
    rows = []
    for batch in batches:
        assert batch.block_type == "table"
        lines = batch.text.splitlines()
        assert lines[:2] == ["| id | name |", "| --- | --- |"]
        sizes.append(len(lines) - 2)
        rows.extend(lines[2:])
    expected_ranges = []
    for first_row in range(2, row_count + 2, batch_size):
        expected_ranges.append((first_row, min(first_row + batch_size - 1, row_count + 1)))
    assert [(batch.metadata["csv_row_start"], batch.metadata["csv_row_end"]) for batch in batches] == expected_ranges
    assert all(batch.metadata["csv_columns"] == ["id", "name"] for batch in batches)
    assert sizes == expected_sizes
    assert rows == [f"| {number:03d} | Person {number} |" for number in range(row_count)]


async def test_13_batches_are_read_on_demand():
    #  quoted multiline cells still count as just one data row.
    document = Document(text='id,note\n1,"first\nsecond"\n2,ok\n3,extra,cell')
    with closing(CsvParser(batch_size=2).iter_batches(document)) as batches:
        first = next(batches)
        assert first.text == "| id | note |\n| --- | --- |\n| 1 | first<br>second |\n| 2 | ok |"
        # The malformed record is consumed only when the next batch is requested.
        with pytest.raises(StopIteration):
            next(batches)


@pytest.mark.parametrize("ending", ["complete", "error", "early_stop", "empty"])
async def test_14_input_stream_is_closed(tmp_path, monkeypatch, ending):
    path = tmp_path / "input.csv"
    text = "id,name\n1,Ali\n2,Bob"
    if ending == "error":
        text = "id,name\n1,Ali\n2,Bob,extra"
    elif ending == "empty":
        text = ""
    path.write_text(text, encoding="utf-8")
    parser = CsvParser(batch_size=1)
    document = Document(source_path=str(path))
    # we keep a reference to the real file stream so its closed state can be checked
    stream = parser._open_stream(document)
    monkeypatch.setattr(parser, "_open_stream", lambda document: stream)
    with closing(parser.iter_batches(document)) as batches:
        if ending == "empty":
            assert list(batches) == []
            assert stream.closed
        else:
            next(batches)
            assert not stream.closed
            if ending == "error":
                assert list(batches) == []
                assert stream.closed
            elif ending == "complete":
                assert len(list(batches)) == 1
                assert stream.closed
            # for early_stop, leaving the closing context just closes the suspended generator
    assert stream.closed


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, "2", None])
async def test_15_invalid_batch_size(batch_size):
    with pytest.raises(ConfigError, match="positive integer"):
        CsvParser(batch_size=batch_size)


async def test_16_parse_collects_batches():
    document = Document(id="batched-file", text="id,name\n1,Ali\n2,Bob\n3,Ela", metadata={"source": "example"})
    result = await CsvParser(batch_size=2).parse(document)
    assert result.document_id == document.id
    assert result.metadata == document.metadata
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 1 | Ali |\n| 2 | Bob |"
    assert result.text_blocks[1].text == "| id | name |\n| --- | --- |\n| 3 | Ela |"
    assert len(result.text_blocks) == 2


async def test_malformed_records_are_skipped_without_losing_valid_rows():
    document = Document(text='id,name\n1,Alice\n2,Bob,\n3,"x"y\n4,Dana\n')

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == ("| id | name |\n| --- | --- |\n| 1 | Alice |\n| 2 | Bob |\n| 4 | Dana |")
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 1
    assert result.text_blocks[0].metadata["csv_row_numbers"] == [2, 3, 5]


async def test_missing_and_extra_trailing_empty_cells_are_normalized():
    document = Document(text="id,name,note\n1,Alice\n2,Bob,,\n3,Cam,ok")

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == (
        "| id | name | note |\n| --- | --- | --- |\n| 1 | Alice |  |\n| 2 | Bob |  |\n| 3 | Cam | ok |"
    )
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 0
    assert result.text_blocks[0].metadata["csv_row_numbers"] == [2, 3, 4]


async def test_header_without_indexable_rows_raises():
    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(Document(text="id,name\n1,Alice,unexpected\n"))

    assert exc_info.value.code == "NO_INDEXABLE_CONTENT"
    assert exc_info.value.extra == {"skipped_rows": 1}


async def test_unclosed_quote_in_header_raises():
    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(Document(text='a,"b\n1,2\n'))

    assert exc_info.value.code == "NO_INDEXABLE_CONTENT"
    assert exc_info.value.extra == {"skipped_rows": 2}


async def test_trailing_empty_header_names_are_preserved_with_generated_names():
    result = await CsvParser().parse(Document(text="a,b,\n1,2,x\n3,4,y\n"))

    assert result.text_blocks[0].text == ("| a | b | column_3 |\n| --- | --- | --- |\n| 1 | 2 | x |\n| 3 | 4 | y |")
    assert result.text_blocks[0].metadata["csv_columns"] == ["a", "b", "column_3"]


async def test_generated_header_names_do_not_collide_with_explicit_names():
    result = await CsvParser().parse(Document(text="a,column_3,\n1,existing,unnamed\n"))

    assert result.text_blocks[0].text == (
        "| a | column_3 | column_3_2 |\n| --- | --- | --- |\n| 1 | existing | unnamed |"
    )
    assert result.text_blocks[0].metadata["csv_columns"] == ["a", "column_3", "column_3_2"]


async def test_unclosed_quote_reports_consumed_physical_lines():
    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(Document(text='id,note\n1,"5 inch screen\n2,ok\n3,also\n'))

    assert exc_info.value.extra == {"skipped_rows": 3}
    assert "starting at physical line 2" in exc_info.value.message
    assert "consumed 3 physical line(s)" in exc_info.value.message


async def test_malformed_quote_reports_all_consumed_lines_and_keeps_later_rows():
    result = await CsvParser().parse(Document(text='id,note\n1,"5 inch screen\n2,ok\n3,"later\n4,good\n'))

    assert result.text_blocks[0].text.endswith("| 4 | good |")
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 3
    assert result.text_blocks[0].metadata["csv_row_numbers"] == [5]


async def test_cell_larger_than_python_csv_default_limit_is_accepted():
    value = "x" * (256 * 1024)

    result = await CsvParser().parse(Document(text=f"id,note\n1,{value}\n"))

    assert value in result.text_blocks[0].text
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 0


async def test_late_cp1252_character_is_not_replaced(tmp_path):
    path = tmp_path / "late-cp1252.csv"
    prefix = "x" * (70 * 1024)
    path.write_bytes(f"id,note\n1,{prefix} café\n".encode("cp1252"))

    result = await CsvParser().parse(Document(source_path=str(path)))

    assert "café" in result.text_blocks[0].text
    assert "\ufffd" not in result.text_blocks[0].text


async def test_valid_utf8_path_does_not_run_chardet(tmp_path, monkeypatch):
    fake_chardet = ModuleType("chardet")

    def fail_if_called(_raw):
        raise AssertionError("chardet must not run for valid UTF-8")

    fake_chardet.detect = fail_if_called
    monkeypatch.setitem(sys.modules, "chardet", fake_chardet)
    path = tmp_path / "valid-utf8.csv"
    path.write_text("id,name\n1,café\n", encoding="utf-8")

    result = await CsvParser().parse(Document(source_path=str(path)))

    assert "café" in result.text_blocks[0].text


async def test_mostly_utf8_path_replaces_a_stray_cp1252_byte(tmp_path, monkeypatch):
    fake_chardet = ModuleType("chardet")

    def fail_if_called(_raw):
        raise AssertionError("chardet must not reinterpret text that already contains valid UTF-8")

    fake_chardet.detect = fail_if_called
    monkeypatch.setitem(sys.modules, "chardet", fake_chardet)
    path = tmp_path / "mixed-encoding.csv"
    utf8_rows = "".join(f"{number},café\n" for number in range(7_000)).encode("utf-8")
    path.write_bytes(b"id,name\n" + utf8_rows + b"7000,Rep\xe9\n")

    result = await CsvParser(batch_size=10_000).parse(Document(source_path=str(path)))

    assert "café" in result.text_blocks[0].text
    assert "Rep\ufffd" in result.text_blocks[0].text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 1


async def test_utf8_french_text_with_a_stray_byte_keeps_utf8(tmp_path, monkeypatch):
    fake_chardet = ModuleType("chardet")

    def fail_if_called(_raw):
        raise AssertionError("chardet must not reinterpret valid UTF-8 accents")

    fake_chardet.detect = fail_if_called
    monkeypatch.setitem(sys.modules, "chardet", fake_chardet)
    path = tmp_path / "mixed-french.csv"
    path.write_bytes("id,note\n1,Le café crème à l’église\n2,".encode() + b"Ren\xe9\n")

    result = await CsvParser().parse(Document(source_path=str(path)))

    assert "Le café crème à l’église" in result.text_blocks[0].text
    assert "Ren\ufffd" in result.text_blocks[0].text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 1


@pytest.mark.parametrize(
    ("utf8_text", "stray_bytes"),
    [
        ("déjà vu", b"it\x92s"),
        ("Hélène,Orléans", b"Ren\xe9"),
    ],
)
@pytest.mark.parametrize("source_kind", ["path", "bytes"])
async def test_small_utf8_file_with_one_stray_byte_keeps_utf8(
    tmp_path, monkeypatch, utf8_text, stray_bytes, source_kind
):
    fake_chardet = ModuleType("chardet")

    def fail_if_called(_raw):
        raise AssertionError("valid UTF-8 evidence must outweigh one stray byte")

    fake_chardet.detect = fail_if_called
    monkeypatch.setitem(sys.modules, "chardet", fake_chardet)
    raw = f'id,note\n1,"{utf8_text}"\n2,'.encode() + stray_bytes + b"\n"
    path = tmp_path / "small-mixed.csv"
    path.write_bytes(raw)
    document = Document(source_path=str(path)) if source_kind == "path" else Document(raw_bytes=raw)

    result = await CsvParser().parse(document)

    text = result.text_blocks[0].text
    assert utf8_text in text
    assert "\ufffd" in text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 1


async def test_tied_utf8_and_invalid_evidence_remains_single_byte_encoded(monkeypatch):
    import chardet

    monkeypatch.setattr(chardet, "detect", lambda _: {"encoding": "Windows-1252", "confidence": 0.99})
    raw = "id,note\n1,Ã©é\n".encode("cp1252")

    result = await CsvParser().parse(Document(raw_bytes=raw))

    assert "Ã©é" in result.text_blocks[0].text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 0


async def test_stray_byte_before_utf8_text_keeps_utf8(tmp_path, monkeypatch):
    fake_chardet = ModuleType("chardet")

    def fail_if_called(_raw):
        raise AssertionError("chardet must not reinterpret valid UTF-8 found after an invalid byte")

    fake_chardet.detect = fail_if_called
    monkeypatch.setitem(sys.modules, "chardet", fake_chardet)
    path = tmp_path / "stray-byte-before-utf8.csv"
    utf8_rows = "".join(f"{number},café\n" for number in range(1, 30_001)).encode()
    path.write_bytes(b"id,note\n0,Ren\xe9\n" + utf8_rows)

    result = await CsvParser(batch_size=40_000).parse(Document(source_path=str(path)))

    text = result.text_blocks[0].text
    assert "Ren\ufffd" in text
    assert text.count("café") == 30_000
    assert "caf\ufffd" not in text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 1


async def test_plain_ascii_prefix_uses_bounded_chardet_sample(tmp_path, monkeypatch):
    samples: list[bytes] = []
    fake_chardet = ModuleType("chardet")

    def detect(raw):
        samples.append(raw)
        return {"encoding": "cp1252", "confidence": 0.99}

    fake_chardet.detect = detect
    monkeypatch.setitem(sys.modules, "chardet", fake_chardet)
    path = tmp_path / "cp1252-after-ascii.csv"
    ascii_rows = "".join(f"{number},plain text\n" for number in range(10_000)).encode()
    path.write_bytes(b"id,name\n" + ascii_rows + b"10000,Ren\xe9\n")

    result = await CsvParser(batch_size=20_000).parse(Document(source_path=str(path)))

    assert "René" in result.text_blocks[0].text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 0
    assert len(samples) == 1
    assert len(samples[0]) <= 4096


@pytest.mark.parametrize("source_kind", ["path", "bytes"])
@pytest.mark.parametrize("accidental_utf8_first", [False, True])
async def test_cp1252_accents_outweigh_accidental_utf8(tmp_path, source_kind, accidental_utf8_first):
    rows = "1,Renée à Noël\n" * 5000
    accidental = "2,RÉSUMÉ…\n"
    body = accidental + rows if accidental_utf8_first else rows + accidental
    raw = ("id,note\n" + body).encode("cp1252")
    path = tmp_path / "real-cp1252.csv"
    path.write_bytes(raw)
    document = Document(source_path=str(path)) if source_kind == "path" else Document(raw_bytes=raw)

    result = await CsvParser().parse(document)

    text = result.text_blocks[0].text
    assert text.count("Renée à Noël") == 5000
    assert "RÉSUMÉ…" in text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 0


async def test_encoding_sample_excludes_long_ascii_runs_and_is_not_duplicated(monkeypatch):
    import chardet

    samples = []

    def detect(sample):
        samples.append(sample)
        return {"encoding": "Windows-1252"}

    monkeypatch.setattr(chardet, "detect", detect)
    for raw in (b"id,note\n1," + b"x" * 70000 + b" caf\xe9\n", b"id,note\n1,caf\xe9\n"):
        result = await CsvParser().parse(Document(raw_bytes=raw))
        assert "café" in result.text_blocks[0].text
    assert len(samples) == 2
    assert len(samples[0]) < 256
    assert samples[1] == b"id,note\n1,caf\xe9\n"


@pytest.mark.parametrize("text", ["café", "東京", "🌍"])
@pytest.mark.parametrize("stray_first", [True, False])
async def test_mixed_utf8_evidence_counts_characters_across_blocks(tmp_path, text, stray_first):
    valid = (f"1,{text}\n" * 10000).encode()
    stray = b"2,Ren\xe9\n"
    path = tmp_path / "mixed.csv"
    path.write_bytes(b"id,note\n" + (stray + valid if stray_first else valid + stray))

    result = await CsvParser(batch_size=20000).parse(Document(source_path=str(path)))

    assert result.text_blocks[0].text.count(text) == 10000
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 1


async def test_low_confidence_encoding_guess_uses_cp1252(monkeypatch):
    import chardet

    monkeypatch.setattr(chardet, "detect", lambda _: {"encoding": "MacCyrillic", "confidence": 0.08})
    result = await CsvParser().parse(Document(raw_bytes="id,note\n1,Renée à Noël\n".encode("cp1252")))

    assert "Renée à Noël" in result.text_blocks[0].text
    assert result.text_blocks[0].metadata["csv_decode_replacements"] == 0
