"""Tests for MOSS transcript output normalization."""

from __future__ import annotations

from services.inference.parsers.moss_transcript import normalize_moss_timestamped_transcript


def test_normalizes_moss_dash_ranges_into_timestamped_speaker_lines():
    transcript = "[1.12-2.32][S01] Hello everyone.[2.68-4.32][S02] This week."

    assert normalize_moss_timestamped_transcript(transcript) == (
        "[00:00:01.120] [S01] Hello everyone. [00:00:02.320]\n[00:00:02.680] [S02] This week. [00:00:04.320]"
    )


def test_normalizes_compact_moss_output_and_defaults_missing_speaker():
    transcript = "[0.41]Bonjour[5.42][5.42][S02]Bienvenue\n sur Twake[8.90]"

    assert normalize_moss_timestamped_transcript(transcript) == (
        "[00:00:00.410] [S01] Bonjour [00:00:05.420]\n[00:00:05.420] [S02] Bienvenue sur Twake [00:00:08.900]"
    )


def test_normalizes_moss_timestamps_across_hour_boundary():
    transcript = "[3599.9995-3601][S01] Long recording."

    assert normalize_moss_timestamped_transcript(transcript) == "[01:00:00.000] [S01] Long recording. [01:00:01.000]"


def test_preserves_unrecognized_or_incomplete_output():
    transcript = "[1.12-2.32][S01] Complete.[2.68-"

    assert normalize_moss_timestamped_transcript(transcript) == transcript
