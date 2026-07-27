"""Tests for indexing aggregation and promptfoo result folding."""

from __future__ import annotations

from core.evaluation.metrics import extract_results, indexing_metrics, summarize
from core.models.evaluation import EvalTestCase, FileIndexingSample


def _sample(name: str, seconds: float, size: int = 1024, failed: bool = False):
    return FileIndexingSample(filename=name, size_bytes=size, duration_seconds=seconds, failed=failed)


def _retrieval_row(query: str, file_ids: list[str], score: float | None = None):
    row = {
        "vars": {"query": query},
        "response": {
            "output": [
                {"content": f"chunk from {fid}", "metadata": {"file_id": fid, "source": fid}} for fid in file_ids
            ]
        },
    }
    if score is not None:
        row["gradingResult"] = {
            "score": score,
            "componentResults": [
                {"score": score, "assertion": {"type": "context-relevance"}},
            ],
        }
    return row


def _answer_row(query: str, answer: str, success: bool, factuality: float = 1.0):
    return {
        "vars": {"query": query},
        "response": {"output": {"answer": answer, "sources": []}},
        "success": success,
        "gradingResult": {
            "pass": success,
            "reason": "graded",
            "componentResults": [
                {"score": factuality, "assertion": {"type": "factuality"}},
                {"score": 0.5, "assertion": {"type": "llm-rubric"}},
            ],
        },
    }


# ── indexing ─────────────────────────────────────────────────────────


def test_throughput_uses_wall_clock_not_summed_durations():
    """Files may be indexed concurrently, so summing per-file durations would
    overstate throughput."""
    metrics = indexing_metrics([_sample("a.pdf", 4.0), _sample("b.pdf", 4.0)], wall_seconds=4.0)
    assert metrics.files_per_minute == 30.0


def test_failed_files_are_counted_but_excluded_from_throughput():
    metrics = indexing_metrics([_sample("a.pdf", 2.0), _sample("b.pdf", 0.0, failed=True)], wall_seconds=2.0)
    assert metrics.files_total == 2
    assert metrics.files_failed == 1
    assert metrics.files_per_minute == 30.0


def test_percentiles_on_a_single_file_return_that_file():
    metrics = indexing_metrics([_sample("a.pdf", 3.0)], wall_seconds=3.0)
    assert metrics.p50_seconds == 3.0
    assert metrics.p95_seconds == 3.0


def test_zero_wall_time_does_not_divide_by_zero():
    metrics = indexing_metrics([_sample("a.pdf", 0.0)], wall_seconds=0.0)
    assert metrics.files_per_minute == 0.0
    assert metrics.megabytes_per_second == 0.0


def test_breakdown_is_grouped_by_lowercased_extension():
    metrics = indexing_metrics(
        [_sample("a.PDF", 2.0), _sample("b.pdf", 4.0), _sample("c.txt", 1.0)],
        wall_seconds=7.0,
    )
    assert metrics.by_extension[".pdf"]["files"] == 2
    assert metrics.by_extension[".pdf"]["mean_seconds"] == 3.0
    assert metrics.by_extension[".txt"]["files"] == 1


# ── promptfoo envelope ───────────────────────────────────────────────


def test_extract_results_accepts_the_nested_v3_envelope():
    rows = extract_results({"results": {"version": 3, "results": [{"vars": {}}]}})
    assert len(rows) == 1


def test_extract_results_accepts_a_bare_list():
    assert len(extract_results([{"vars": {}}, {"vars": {}}])) == 2


def test_extract_results_tolerates_an_unexpected_shape():
    assert extract_results({"unexpected": True}) == []
    assert extract_results(None) == []


# ── summarize ────────────────────────────────────────────────────────


def test_ranking_metrics_use_the_first_matching_rank():
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("gold.pdf",))]
    retrieval, _, details = summarize(
        cases=cases,
        retrieval_payload=[_retrieval_row("q1", ["noise.pdf", "gold.pdf"])],
        answer_payload=[],
    )
    assert retrieval.hit_rate == 1.0
    assert retrieval.mrr == 0.5
    assert retrieval.recall == 1.0
    assert details[0].reciprocal_rank == 0.5


def test_a_miss_scores_zero_across_the_ranking_metrics():
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("gold.pdf",))]
    retrieval, _, details = summarize(
        cases=cases,
        retrieval_payload=[_retrieval_row("q1", ["noise.pdf"])],
        answer_payload=[],
    )
    assert retrieval.hit_rate == 0.0
    assert retrieval.mrr == 0.0
    assert details[0].hit is False


def test_cases_without_ground_truth_sources_are_skipped_not_failed():
    """A sparsely-annotated test set must not read as a broken retriever."""
    cases = [
        EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("gold.pdf",)),
        EvalTestCase(query="q2", expected_answer="b"),
    ]
    retrieval, _, _ = summarize(
        cases=cases,
        retrieval_payload=[
            _retrieval_row("q1", ["gold.pdf"]),
            _retrieval_row("q2", ["whatever.pdf"]),
        ],
        answer_payload=[],
    )
    assert retrieval.scored_cases == 1
    assert retrieval.skipped_cases == 1
    assert retrieval.hit_rate == 1.0


def test_recall_is_the_fraction_of_expected_sources_found():
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("a.pdf", "b.pdf"))]
    retrieval, _, _ = summarize(
        cases=cases,
        retrieval_payload=[_retrieval_row("q1", ["a.pdf", "z.pdf"])],
        answer_payload=[],
    )
    assert retrieval.recall == 0.5


def test_context_relevance_is_averaged_when_present():
    cases = [
        EvalTestCase(query="q1", expected_answer="a"),
        EvalTestCase(query="q2", expected_answer="b"),
    ]
    retrieval, _, _ = summarize(
        cases=cases,
        retrieval_payload=[
            _retrieval_row("q1", ["a.pdf"], score=1.0),
            _retrieval_row("q2", ["b.pdf"], score=0.0),
        ],
        answer_payload=[],
    )
    assert retrieval.context_relevance == 0.5


def test_context_relevance_is_none_when_no_row_carried_a_grade():
    cases = [EvalTestCase(query="q1", expected_answer="a")]
    retrieval, _, _ = summarize(cases=cases, retrieval_payload=[_retrieval_row("q1", ["a.pdf"])], answer_payload=[])
    assert retrieval.context_relevance is None


def test_answer_metrics_average_pass_rate_and_component_scores():
    cases = [
        EvalTestCase(query="q1", expected_answer="a"),
        EvalTestCase(query="q2", expected_answer="b"),
    ]
    _, answer, details = summarize(
        cases=cases,
        retrieval_payload=[],
        answer_payload=[
            _answer_row("q1", "correct", True, factuality=1.0),
            _answer_row("q2", "wrong", False, factuality=0.0),
        ],
    )
    assert answer.scored_cases == 2
    assert answer.pass_rate == 0.5
    assert answer.factuality == 0.5
    assert answer.rubric_score == 0.5
    assert details[0].answer == "correct"
    assert details[1].answer_passed is False
    assert details[0].grader_reason == "graded"


def test_missing_rows_leave_the_case_unscored_rather_than_crashing():
    """promptfoo can drop a row on a provider error; the run still reports."""
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("gold.pdf",))]
    retrieval, answer, details = summarize(cases=cases, retrieval_payload=[], answer_payload=[])
    assert retrieval.scored_cases == 1
    assert retrieval.hit_rate == 0.0
    assert answer.scored_cases == 0
    assert details[0].answer is None


def test_ground_truth_matches_the_original_filename_when_the_file_id_was_sanitised():
    """The indexer rewrites 'A B.pdf' to 'A_B.pdf' because the API rejects
    spaces in a file_id — a test set still names the real file, so matching
    falls back to metadata.source."""
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("A B.pdf",))]
    row = {
        "vars": {"query": "q1"},
        "response": {"output": [{"content": "chunk", "metadata": {"file_id": "A_B.pdf", "source": "/data/A B.pdf"}}]},
    }
    retrieval, _, details = summarize(cases=cases, retrieval_payload=[row], answer_payload=[])

    assert retrieval.hit_rate == 1.0
    assert retrieval.recall == 1.0
    # Display uses the file_id — `source` is a server-side storage path.
    assert details[0].retrieved_file_ids == ["A_B.pdf"]


def test_ground_truth_still_matches_a_sanitised_file_id_directly():
    """Authors who wrote the sanitised id are not punished for it."""
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("A_B.pdf",))]
    row = {
        "vars": {"query": "q1"},
        "response": {"output": [{"content": "chunk", "metadata": {"file_id": "A_B.pdf", "source": "/data/A B.pdf"}}]},
    }
    retrieval, _, _ = summarize(cases=cases, retrieval_payload=[row], answer_payload=[])

    assert retrieval.hit_rate == 1.0


def test_matching_survives_file_id_sanitisation_on_either_side():
    """The indexer stores 'A_B.pdf' for a file named 'A B.pdf'. Whichever form
    the metadata carries, and whichever the author wrote, must match — this is
    what silently zeroed the ranking metrics on the first real run."""
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("A B.pdf",))]
    for metadata in (
        {"file_id": "A_B.pdf", "source": "A_B.pdf"},
        {"file_id": "A_B.pdf", "source": "/data/A B.pdf"},
        {"file_id": "A_B.pdf"},
    ):
        row = {"vars": {"query": "q1"}, "response": {"output": [{"content": "c", "metadata": metadata}]}}
        retrieval, _, _ = summarize(cases=cases, retrieval_payload=[row], answer_payload=[])
        assert retrieval.hit_rate == 1.0, metadata


def test_retrieved_documents_are_named_by_file_id_not_the_storage_path():
    """metadata.source is the server's temp path ('/app/data/17851..._x.pdf'),
    which is meaningless in the per-question table."""
    cases = [EvalTestCase(query="q1", expected_answer="a", expected_file_ids=("report.pdf",))]
    row = {
        "vars": {"query": "q1"},
        "response": {
            "output": [
                {
                    "content": "c",
                    "metadata": {
                        "file_id": "report.pdf",
                        "source": "/app/data/1785151308957_c270_report.pdf",
                    },
                }
            ]
        },
    }
    _, _, details = summarize(cases=cases, retrieval_payload=[row], answer_payload=[])

    assert details[0].retrieved_file_ids == ["report.pdf"]
