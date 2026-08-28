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
_SPEAKER = r"S\d+"

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
    segments = _parse_moss_segments(transcript)
    if not segments:
        return transcript
    return "\n".join(
        f"[{_format_timestamp(segment.start)}] [{segment.speaker}] {segment.text} [{_format_timestamp(segment.end)}]"
        for segment in segments
    )


def _parse_moss_segments(transcript: str) -> list[_MossSegment]:
    for pattern, default_speaker in ((_DASH_SEGMENT, None), (_COMPACT_SEGMENT, "S01")):
        segments = _parse_segments_with_pattern(transcript, pattern, default_speaker=default_speaker)
        if segments:
            return segments
    return []


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
        segments.append(_MossSegment(start=start, end=end, speaker=speaker, text=text))
        cursor = match.end()

    return segments if not transcript[cursor:].strip() else []


def _format_timestamp(seconds: float) -> str:
    milliseconds_total = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}.{milliseconds:03d}"


__all__ = ["normalize_moss_timestamped_transcript"]
