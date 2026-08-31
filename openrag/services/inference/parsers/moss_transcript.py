"""Safe normalization for MOSS diarized transcription output.

MOSS can return either compact ``[start-end][Sxx] text`` segments or the
equivalent ``[start][Sxx] text [end]`` form. This module is intentionally
independent of the MOSS client package so OpenRAG deployments do not need that
package merely to use the OpenAI-compatible endpoint.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_TIMESTAMP = r"\d+(?:\.\d+)?"
_CLOCK_TIMESTAMP = r"\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?"
_SPEAKER = r"[Ss]\d+"

_DASH_SEGMENT = re.compile(
    rf"\[\s*(?P<start>{_TIMESTAMP})\s*-\s*(?P<end>{_TIMESTAMP})\s*\]"
    rf"\s*\[\s*(?P<speaker>{_SPEAKER})\s*\]\s*"
    rf"(?P<text>.*?)"
    rf"(?=\s*\[\s*{_TIMESTAMP}\s*-\s*{_TIMESTAMP}\s*\]\s*\[\s*{_SPEAKER}\s*\]|\s*\Z)",
    re.DOTALL,
)
_COMPACT_SEGMENT = re.compile(
    rf"\[\s*(?P<start>{_TIMESTAMP})\s*\]\s*"
    rf"(?:\[\s*(?P<speaker>{_SPEAKER})\s*\]\s*)?"
    rf"(?P<text>.*?)"
    rf"\[\s*(?P<end>{_TIMESTAMP})\s*\]"
    rf"(?=\s*(?:\[\s*{_TIMESTAMP}\s*\](?:\s*\[\s*{_SPEAKER}\s*\])?|\Z))",
    re.DOTALL,
)
# A trailing range without its speaker label is an incomplete MOSS segment,
# whether its closing bracket has arrived or not. Treat it as a parser mismatch
# so the caller receives the original response instead of partial normalization.
_INCOMPLETE_DASH_RANGE = re.compile(r"\[\s*\d+(?:\.\d+)?\s*-\s*(?:\d+(?:\.\d*)?)?\s*\]?\s*$")
_TIMECODE = re.compile(
    rf"\[\s*(?:{_CLOCK_TIMESTAMP}|{_TIMESTAMP})"
    rf"(?:\s*-\s*(?:{_CLOCK_TIMESTAMP}|{_TIMESTAMP}))?\s*\]"
)
_SPEAKER_LABEL = re.compile(rf"\[\s*(?P<speaker>{_SPEAKER})\s*\]")


@dataclass(frozen=True, slots=True)
class _MossSegment:
    start: float
    end: float
    speaker: str
    text: str


def normalize_moss_timestamped_transcript(transcript: str) -> str:
    """Render valid MOSS diarized output as one timestamped line per segment.

    A parser mismatch must never discard an STT response. When the full text
    cannot be recognized as MOSS output, the original transcription is
    returned unchanged.
    """
    if _has_mixed_segment_syntax(transcript):
        return transcript
    segments = _parse_moss_segments(transcript)
    if not segments:
        return transcript
    return "\n".join(
        f"[{_format_timestamp(segment.start)}] [{segment.speaker}] {segment.text} [{_format_timestamp(segment.end)}]"
        for segment in segments
    )


def normalize_moss_speaker_aware_transcript(transcript: str) -> str:
    """Remove MOSS timecodes and hide labels when it found one speaker.

    MOSS is still asked to diarize every turn. That makes the decision
    deterministic after transcription: ``S1`` and ``S01`` are one speaker,
    so all labels are removed; two or more distinct IDs retain normalized
    ``[Sxx]`` labels. Raw output that cannot be parsed as timestamped MOSS
    segments is handled conservatively, preserving its text and line order.
    """
    if _has_mixed_segment_syntax(transcript):
        return transcript
    segments = _parse_moss_segments(transcript)
    if segments:
        speakers = {segment.speaker for segment in segments}
        include_speakers = len(speakers) > 1
        return "\n".join(
            f"[{segment.speaker}] {segment.text}" if include_speakers else segment.text for segment in segments
        )
    return _normalize_unparsed_speaker_aware_transcript(transcript)


def _parse_moss_segments(transcript: str) -> list[_MossSegment]:
    for pattern, default_speaker in ((_DASH_SEGMENT, None), (_COMPACT_SEGMENT, "S01")):
        segments = _parse_segments_with_pattern(transcript, pattern, default_speaker=default_speaker)
        if segments:
            return segments
    return []


def _has_mixed_segment_syntax(transcript: str) -> bool:
    """Reject a response that combines the two MOSS segment encodings.

    Each parser intentionally consumes only one complete encoding. Treating a
    compact turn as text inside a dash-range match would silently discard its
    speaker boundary, so leave the original response untouched instead.
    """
    return _DASH_SEGMENT.search(transcript) is not None and _COMPACT_SEGMENT.search(transcript) is not None


def _parse_segments_with_pattern(
    transcript: str,
    pattern: re.Pattern[str],
    *,
    default_speaker: str | None,
) -> list[_MossSegment]:
    matches = list(pattern.finditer(transcript))
    if not matches:
        return []

    segments: list[_MossSegment] = []
    cursor = 0
    for match in matches:
        if transcript[cursor : match.start()].strip():
            return []
        start = float(match.group("start"))
        end = float(match.group("end"))
        speaker = match.group("speaker") or default_speaker
        text = " ".join(match.group("text").split())
        if (
            not (math.isfinite(start) and math.isfinite(end))
            or start < 0
            or end < start
            or not speaker
            or not text
            or _INCOMPLETE_DASH_RANGE.search(text)
        ):
            return []
        segments.append(_MossSegment(start=start, end=end, speaker=_normalize_speaker_id(speaker), text=text))
        cursor = match.end()

    return segments if not transcript[cursor:].strip() else []


def _format_timestamp(seconds: float) -> str:
    milliseconds_total = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}.{milliseconds:03d}"


def _normalize_speaker_id(speaker: str) -> str:
    """Canonicalize equivalent MOSS IDs such as ``S1`` and ``S01``."""
    return f"S{int(speaker[1:]):02d}"


def _normalize_unparsed_speaker_aware_transcript(transcript: str) -> str:
    """Clean MOSS-like raw output without losing an unrecognized response."""
    if _INCOMPLETE_DASH_RANGE.search(transcript):
        return transcript

    without_timecodes = _TIMECODE.sub("", transcript)
    labels = list(_SPEAKER_LABEL.finditer(without_timecodes))
    if not labels:
        return _normalize_lines(without_timecodes) or transcript

    turns: list[tuple[str | None, str]] = []
    leading_text = _normalize_text(without_timecodes[: labels[0].start()])
    if leading_text:
        turns.append((None, leading_text))
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(without_timecodes)
        text = _normalize_text(without_timecodes[label.end() : end])
        if text:
            turns.append((_normalize_speaker_id(label.group("speaker")), text))

    if not turns:
        return transcript
    speakers = {speaker for speaker, _ in turns if speaker is not None}
    include_speakers = len(speakers) > 1
    return "\n".join(f"[{speaker}] {text}" if include_speakers and speaker else text for speaker, text in turns)


def _normalize_lines(text: str) -> str:
    return "\n".join(line for raw_line in text.splitlines() if (line := _normalize_text(raw_line)))


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


__all__ = ["normalize_moss_speaker_aware_transcript", "normalize_moss_timestamped_transcript"]
