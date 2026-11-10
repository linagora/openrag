from __future__ import annotations

import runpy
import sys
import time
from pathlib import Path

import httpx
import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "data_indexer.py"


class _Response:
    def __init__(self, status_code: int, body: dict[str, str]) -> None:
        self.status_code = status_code
        self.text = str(body)
        self._body = body

    def json(self) -> dict[str, str]:
        return self._body


def _prepare_script(monkeypatch, directory: Path, post, *, sleep=None) -> None:
    def get(url: str, **_kwargs):
        return _Response(200 if url.endswith("/health_check") else 404, {})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(time, "sleep", sleep or (lambda _seconds: None))
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_indexer.py", "--dir", str(directory), "--partition", "default"],
    )


def test_data_indexer_retries_the_same_file_after_outcome_backlog(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "report.pdf").write_bytes(b"pdf contents")
    responses = iter(
        [
            _Response(503, {"detail": "[INDEXING_OUTCOME_BACKLOG]: retry shortly"}),
            _Response(201, {"task_id": "task-1"}),
        ]
    )
    uploads = []

    def post(_url, *, files, **_kwargs):
        filename, stream, _content_type = files["file"]
        uploads.append((filename, stream.read()))
        return next(responses)

    _prepare_script(monkeypatch, source, post)

    runpy.run_path(str(_SCRIPT), run_name="__main__")

    assert uploads == [("report.pdf", b"pdf contents"), ("report.pdf", b"pdf contents")]


def test_data_indexer_stops_with_error_when_backlog_does_not_clear(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "first.pdf").write_bytes(b"first")
    (source / "next.pdf").write_bytes(b"next")
    uploads = []
    retry_delays = []

    def post(_url, *, files, **_kwargs):
        filename, stream, _content_type = files["file"]
        uploads.append((filename, stream.read()))
        return _Response(503, {"detail": "[INDEXING_OUTCOME_BACKLOG]: retry shortly"})

    _prepare_script(monkeypatch, source, post, sleep=retry_delays.append)

    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(_SCRIPT), run_name="__main__")

    assert error.value.code != 0
    assert len({filename for filename, _contents in uploads}) == 1
    assert len(uploads) == 9
    assert retry_delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]


def test_data_indexer_does_not_retry_unrelated_service_unavailable(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "report.pdf").write_bytes(b"pdf contents")
    uploads = []

    def post(_url, *, files, **_kwargs):
        uploads.append(files["file"][0])
        return _Response(503, {"detail": "[SERVICE_UNAVAILABLE]: service is unavailable"})

    _prepare_script(monkeypatch, source, post)

    runpy.run_path(str(_SCRIPT), run_name="__main__")

    assert uploads == ["report.pdf"]
