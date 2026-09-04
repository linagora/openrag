"""Conservative normalization for MOSS diarized transcription output."""

from __future__ import annotations

import re
from decimal import Decimal

_SECONDS = r"\d+(?:\.\d+)?"
_CLOCK = r"\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?"
_TIME = rf"(?:{_CLOCK}|{_SECONDS})"
_TIME_TOKEN = rf"\[\s*{_TIME}\s*\]"
_SPEAKER = r"[Ss]\d+"

# MOSS documents and emits dash ranges in seconds. Clock-formatted dash ranges
# remain unsupported so an unfamiliar provider response is preserved verbatim.
_DASH_START = re.compile(
    rf"\[\s*(?P<start>{_SECONDS})\s*-\s*(?P<end>{_SECONDS})\s*\]\s*"
    rf"\[\s*(?P<speaker>{_SPEAKER})\s*\]",
)
_DASH_MARKER = re.compile(rf"\[\s*{_TIME}\s*-")
_UNSUPPORTED_CLOCK_DASH_MARKER = re.compile(rf"\[\s*{_CLOCK}\s*-")
_DASH_CONTENT_MARKER = re.compile(rf"{_TIME_TOKEN}|\[\s*(?:[Ss]\d*|\d+(?:\.\d+)?\s*-)")
_COMPACT_START = re.compile(
    rf"(?P<start_token>\[\s*(?P<start>{_TIME})\s*\])\s*\[\s*(?P<speaker>{_SPEAKER})\s*\]",
)

_INITIAL_TIME = re.compile(rf"^\s*\[\s*(?P<start>{_TIME})\s*\]")
_TRAILING_TIME = re.compile(rf"\[\s*(?P<end>{_TIME})\s*\]\s*$")
_ADJACENT_TIMES = re.compile(rf"{_TIME_TOKEN}\s*{_TIME_TOKEN}")
_COMPACT_BOUNDARY_PAIR = re.compile(
    rf"\[\s*(?P<end>{_TIME})\s*\]\s*\[\s*(?P<start>{_TIME})\s*\]",
)
_TIME_TOKEN_MARKER = re.compile(rf"\[\s*(?P<time>{_TIME})\s*\]")
_SPEAKER_LABEL = re.compile(rf"\[\s*(?P<speaker>{_SPEAKER})\s*\]")
_SPEAKER_MARKER = re.compile(r"\[\s*[Ss]\d*")

_Segment = tuple[str, str]


def normalize_moss_speaker_aware_transcript(transcript: str) -> str:
    """Remove boundaries only when the complete MOSS syntax is recognized."""
    for parser in (_parse_dash, _parse_compact, _parse_speakerless_compact, _parse_speaker_only):
        segments = parser(transcript)
        if segments:
            break
    else:
        return transcript

    include_speakers = len({speaker for speaker, _ in segments}) > 1
    return "\n".join(f"[{speaker}] {text}" if include_speakers else text for speaker, text in segments)


def _parse_dash(transcript: str) -> list[_Segment]:
    starts = list(_DASH_START.finditer(transcript))
    if not starts or transcript[: starts[0].start()].strip() or _UNSUPPORTED_CLOCK_DASH_MARKER.search(transcript):
        return []

    segments: list[_Segment] = []
    for index, match in enumerate(starts):
        if Decimal(match["end"]) < Decimal(match["start"]):
            return []
        end = starts[index + 1].start() if index + 1 < len(starts) else len(transcript)
        text = _normalize_text(transcript[match.end() : end])
        if not text or _DASH_CONTENT_MARKER.search(text):
            return []
        segments.append((_normalize_speaker(match["speaker"]), text))
    return segments


def _parse_compact(transcript: str) -> list[_Segment]:
    starts = list(_COMPACT_START.finditer(transcript))
    if not starts or _DASH_MARKER.search(transcript):
        return []

    regions: list[tuple[str, str, int, int]] = []
    initial = _INITIAL_TIME.match(transcript)
    if initial and initial.end() <= starts[0].start():
        initial_end = _compact_region_end(transcript, initial.end(), starts[0])
        regions.append(("S01", initial["start"], initial.end(), initial_end))
    elif transcript[: starts[0].start()].strip():
        return []

    for index, match in enumerate(starts):
        end = (
            _compact_region_end(transcript, match.end(), starts[index + 1])
            if index + 1 < len(starts)
            else len(transcript)
        )
        regions.append((match["speaker"], match["start"], match.end(), end))

    segments: list[_Segment] = []
    for speaker, start_time, start, end in regions:
        region = transcript[start:end]
        trailing = _TRAILING_TIME.search(region)
        if trailing is None or not _valid_range(start_time, trailing["end"]):
            return []
        text = _normalize_text(region[: trailing.start()])
        if not text or _ADJACENT_TIMES.search(text) or _SPEAKER_MARKER.search(text):
            return []
        segments.append((_normalize_speaker(speaker), text))
    return segments


def _parse_speakerless_compact(transcript: str) -> list[_Segment]:
    """Recognize complete speakerless compact turns with shared boundaries."""
    initial = _INITIAL_TIME.match(transcript)
    trailing = _TRAILING_TIME.search(transcript)
    if initial is None or trailing is None or initial.end() > trailing.start() or _DASH_MARKER.search(transcript):
        return []

    if _has_overlapping_boundary_candidates(transcript, initial.end(), trailing.start()):
        return []

    start = _seconds(initial["start"])
    if start is None:
        return []

    segments: list[_Segment] = []
    cursor = initial.end()

    for boundary in _COMPACT_BOUNDARY_PAIR.finditer(
        transcript,
        cursor,
        trailing.start(),
    ):
        end = _seconds(boundary["end"])
        next_start = _seconds(boundary["start"])
        text = _normalize_text(transcript[cursor : boundary.start()])

        if (
            end is None
            or next_start is None
            or end < start
            or end != next_start
            or not text
            or _SPEAKER_MARKER.search(text)
        ):
            return []

        segments.append(("S01", text))
        start = next_start
        cursor = boundary.end()

    end = _seconds(trailing["end"])
    text = _normalize_text(transcript[cursor : trailing.start()])
    if end is None or end < start or not text or _SPEAKER_MARKER.search(text):
        return []

    segments.append(("S01", text))
    return segments


def _has_overlapping_boundary_candidates(
    transcript: str,
    start: int,
    end: int,
) -> bool:
    tokens = list(_TIME_TOKEN_MARKER.finditer(transcript, start, end))

    for first, second, third in zip(tokens, tokens[1:], tokens[2:]):
        if transcript[first.end() : second.start()].strip() or transcript[second.end() : third.start()].strip():
            continue

        values = [_seconds(token["time"]) for token in (first, second, third)]
        if values[0] is not None and values[0] == values[1] == values[2]:
            return True

    return False


def _compact_region_end(transcript: str, text_start: int, next_start: re.Match[str]) -> int:
    end = next_start.start()
    if _TRAILING_TIME.search(transcript[text_start:end]) is None:
        return next_start.end("start_token")
    return end


def _parse_speaker_only(transcript: str) -> list[_Segment]:
    labels = list(_SPEAKER_LABEL.finditer(transcript))
    if (
        not labels
        or len(labels) != len(_SPEAKER_MARKER.findall(transcript))
        or transcript[: labels[0].start()].strip()
        or _DASH_MARKER.search(transcript)
        or _COMPACT_START.search(transcript)
        or _TIME_TOKEN_MARKER.search(transcript)
    ):
        return []

    segments: list[_Segment] = []
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(transcript)
        text = _normalize_text(transcript[label.end() : end])
        if not text:
            return []
        segments.append((_normalize_speaker(label["speaker"]), text))
    return segments


def _normalize_speaker(speaker: str) -> str:
    digits = speaker[1:].lstrip("0") or "0"
    return f"S{digits.zfill(2)}"


def _valid_range(start: str, end: str) -> bool:
    start_seconds = _seconds(start)
    end_seconds = _seconds(end)
    return start_seconds is not None and end_seconds is not None and end_seconds >= start_seconds


def _seconds(value: str) -> Decimal | None:
    if ":" not in value:
        return Decimal(value)
    hours, minutes, seconds = value.replace(",", ".").split(":")
    if int(minutes) >= 60 or Decimal(seconds) >= 60:
        return None
    return Decimal(hours) * 3600 + Decimal(minutes) * 60 + Decimal(seconds)


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


__all__ = ["normalize_moss_speaker_aware_transcript"]
