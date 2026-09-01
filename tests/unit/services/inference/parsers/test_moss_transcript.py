"""Tests for conservative MOSS transcript normalization."""

from __future__ import annotations

import pytest
from services.inference.parsers.moss_transcript import normalize_moss_speaker_aware_transcript


def test_removes_timecodes_and_single_speaker_labels_from_dash_ranges():
    transcript = "[1.12-2.32][S1] Hello everyone.[2.68-4.32][S01] This week."

    assert normalize_moss_speaker_aware_transcript(transcript) == "Hello everyone.\nThis week."


def test_keeps_normalized_labels_for_multiple_speakers():
    transcript = "[1.12-2.32][S1] Hello everyone.[2.68-4.32][S02] This week."

    assert normalize_moss_speaker_aware_transcript(transcript) == "[S01] Hello everyone.\n[S02] This week."


def test_normalizes_compact_output_with_an_initial_speakerless_turn():
    transcript = "[0.41]Bonjour[5.42][5.42][S02]Bienvenue\n sur Twake[8.90]"

    assert normalize_moss_speaker_aware_transcript(transcript) == "[S01] Bonjour\n[S02] Bienvenue sur Twake"


def test_compact_boundaries_do_not_consume_spoken_bracketed_numbers():
    transcript = "[1][S01] Read section [2] [3][3][S02] Next [4]"

    assert normalize_moss_speaker_aware_transcript(transcript) == "[S01] Read section [2]\n[S02] Next"


def test_normalizes_clock_timecodes():
    transcript = "[00:00:01.120] [S01] Hello everyone. [00:00:02.320]\n[00:00:02.680] [S1] This week. [00:00:04.320]"

    assert normalize_moss_speaker_aware_transcript(transcript) == "Hello everyone.\nThis week."


def test_normalizes_speaker_only_lines():
    transcript = "[S1] Hello everyone.\n[S01] This is still the same speaker."

    assert normalize_moss_speaker_aware_transcript(transcript) == "Hello everyone.\nThis is still the same speaker."


def test_preserves_spoken_bracketed_numbers_in_a_complete_turn():
    transcript = "[00:00:01.000] [S01] The [2024] roadmap is ready. [00:00:02.000]"

    assert normalize_moss_speaker_aware_transcript(transcript) == "The [2024] roadmap is ready."


@pytest.mark.parametrize(
    "transcript",
    [
        "The [2024] roadmap is ready.",
        "[1][S01] Hello",
        "[1][S01] A [2][2] B [3][3][S02] C [4]",
        "[4-2][S01] Hello",
        "[1-2][S01] Hello [2][S02] there [3]",
        "[1] Hello [2][2-3][S02] There",
        "[1-2][S01] Hello [2][S02] there",
        "[1-2][S01] Hello [2][S02",
        "[1-2][S01] Hello [3-4][S02",
        "[1-2][S01] Hello [3-4][S02]",
        "[1.12-2.32][S01] Complete.[2.68-",
        "[1.12-2.32][S01] Complete.[2.68-4.32]",
    ],
)
def test_preserves_unrecognized_ambiguous_or_incomplete_output(transcript):
    assert normalize_moss_speaker_aware_transcript(transcript) == transcript
