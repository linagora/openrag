"""Conservative normalization for MOSS diarized transcription output."""

from __future__ import annotations

import re
from decimal import Decimal

_SECONDS = r"\d+(?:\.\d+)?"
_CLOCK = r"\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?"
_TIME = rf"(?:{_CLOCK}|{_SECONDS})"
_TIME_TOKEN = rf"\[\s*{_TIME}\s*\]"
_SPEAKER = r"[Ss]\d+"

_DASH_START = re.compile(
    rf"\[\s*(?P<start>{_SECONDS})\s*-\s*(?P<end>{_SECONDS})\s*\]\s*"
    rf"\[\s*(?P<speaker>{_SPEAKER})\s*\]",
)
_DASH_MARKER = re.compile(r"\[\s*\d+(?:\.\d+)?\s*-")
_DASH_CONTENT_MARKER = re.compile(rf"{_TIME_TOKEN}|\[\s*(?:[Ss]\d*|\d+(?:\.\d+)?\s*-)")
_COMPACT_START = re.compile(
    rf"(?P<start_token>\[\s*(?P<start>{_TIME})\s*\])\s*\[\s*(?P<speaker>{_SPEAKER})\s*\]",
)
_INITIAL_TIME = re.compile(rf"^\s*\[\s*(?P<start>{_TIME})\s*\]")
_TRAILING_TIME = re.compile(rf"\[\s*(?P<end>{_TIME})\s*\]\s*$")
_ADJACENT_TIMES = re.compile(rf"{_TIME_TOKEN}\s*{_TIME_TOKEN}")
_TIME_TOKEN_MARKER = re.compile(_TIME_TOKEN)
_SPEAKER_LABEL = re.compile(rf"\[\s*(?P<speaker>{_SPEAKER})\s*\]")
_SPEAKER_MARKER = re.compile(r"\[\s*[Ss]\d*")

_Segment = tuple[str, str]


def normalize_moss_speaker_aware_transcript(transcript: str) -> str:
    """Remove boundaries only when the complete MOSS syntax is recognized."""
    for parser in (_parse_dash, _parse_compact, _parse_speaker_only):
        segments = parser(transcript)
        if segments:
            break
    else:
        return transcript

    include_speakers = len({speaker for speaker, _ in segments}) > 1
    return "\n".join(f"[{speaker}] {text}" if include_speakers else text for speaker, text in segments)


def _parse_dash(transcript: str) -> list[_Segment]:
    starts = list(_DASH_START.finditer(transcript))
    if not starts or transcript[: starts[0].start()].strip():
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
        initial_end = starts[0].start()
        if _TRAILING_TIME.search(transcript[initial.end() : initial_end]) is None:
            initial_end = starts[0].end("start_token")
        regions.append(("S01", initial["start"], initial.end(), initial_end))
    elif transcript[: starts[0].start()].strip():
        return []

    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(transcript)
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
