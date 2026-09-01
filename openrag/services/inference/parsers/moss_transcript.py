"""Conservative normalization for MOSS diarized transcription output."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SECONDS = r"\d+(?:\.\d+)?"
_CLOCK = r"\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?"
_TIME = rf"(?:{_CLOCK}|{_SECONDS})"
_TIME_TOKEN = rf"\[\s*{_TIME}\s*\]"
_SPEAKER = r"[Ss]\d+"

_DASH_START = re.compile(
    rf"\[\s*(?P<start>{_SECONDS})\s*-\s*(?P<end>{_SECONDS})\s*\]\s*"
    rf"\[\s*(?P<speaker>{_SPEAKER})\s*\]",
)
_DASH_TOKEN = re.compile(rf"\[\s*{_SECONDS}\s*-\s*{_SECONDS}\s*\]")
_PARTIAL_DASH_TOKEN = re.compile(r"\[\s*\d+(?:\.\d+)?\s*-\s*(?:\d+(?:\.\d*)?)?\s*\]?")
_COMPACT_START = re.compile(
    rf"{_TIME_TOKEN}\s*\[\s*(?P<speaker>{_SPEAKER})\s*\]",
)
_INITIAL_TIME = re.compile(rf"^\s*{_TIME_TOKEN}")
_TRAILING_TIME = re.compile(rf"(?P<end>{_TIME_TOKEN})\s*$")
_ADJACENT_TIME_TOKENS = re.compile(rf"{_TIME_TOKEN}\s*{_TIME_TOKEN}")
_SPEAKER_LABEL = re.compile(rf"\[\s*(?P<speaker>{_SPEAKER})\s*\]")
_PARTIAL_SPEAKER_LABEL = re.compile(r"\[\s*[Ss]\d*\s*\]?\s*$")


@dataclass(frozen=True, slots=True)
class _MossSegment:
    speaker: str
    text: str


def normalize_moss_speaker_aware_transcript(transcript: str) -> str:
    """Remove timecodes from complete MOSS turns and retain useful labels.

    A response is normalized only when one supported syntax consumes it in
    full. Ambiguous or incomplete output is returned unchanged so transcript
    content is never mistaken for a MOSS boundary.
    """
    segments = _parse_dash_segments(transcript)
    if not segments:
        segments = _parse_compact_segments(transcript)
    if not segments:
        segments = _parse_speaker_only_segments(transcript)
    if not segments:
        return transcript

    include_speakers = len({segment.speaker for segment in segments}) > 1
    return "\n".join(
        f"[{segment.speaker}] {segment.text}" if include_speakers else segment.text for segment in segments
    )


def _parse_dash_segments(transcript: str) -> list[_MossSegment]:
    starts = list(_DASH_START.finditer(transcript))
    if not starts or transcript[: starts[0].start()].strip() or _PARTIAL_SPEAKER_LABEL.search(transcript):
        return []

    segments: list[_MossSegment] = []
    for index, start in enumerate(starts):
        if float(start.group("end")) < float(start.group("start")):
            return []
        end = starts[index + 1].start() if index + 1 < len(starts) else len(transcript)
        text = _normalize_text(transcript[start.end() : end])
        if not text or _DASH_TOKEN.search(text) or _PARTIAL_DASH_TOKEN.search(text) or _COMPACT_START.search(text):
            return []
        segments.append(_MossSegment(_normalize_speaker_id(start.group("speaker")), text))
    return segments


def _parse_compact_segments(transcript: str) -> list[_MossSegment]:
    starts = list(_COMPACT_START.finditer(transcript))
    if not starts or _DASH_TOKEN.search(transcript) or _PARTIAL_DASH_TOKEN.search(transcript):
        return []

    regions: list[tuple[str, int, int]] = []
    initial_time = _INITIAL_TIME.match(transcript)
    if initial_time and initial_time.end() <= starts[0].start():
        regions.append(("S01", initial_time.end(), starts[0].start()))
    elif transcript[: starts[0].start()].strip():
        return []

    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(transcript)
        regions.append((start.group("speaker"), start.end(), end))

    segments: list[_MossSegment] = []
    for speaker, start, end in regions:
        region = transcript[start:end]
        trailing_time = _TRAILING_TIME.search(region)
        if trailing_time is None:
            return []
        text = _normalize_text(region[: trailing_time.start()])
        if not text or _ADJACENT_TIME_TOKENS.search(text) or _COMPACT_START.search(text) or _DASH_TOKEN.search(text):
            return []
        segments.append(_MossSegment(_normalize_speaker_id(speaker), text))
    return segments


def _parse_speaker_only_segments(transcript: str) -> list[_MossSegment]:
    labels = list(_SPEAKER_LABEL.finditer(transcript))
    if not labels or transcript[: labels[0].start()].strip() or _PARTIAL_SPEAKER_LABEL.search(transcript):
        return []

    segments: list[_MossSegment] = []
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(transcript)
        text = _normalize_text(transcript[label.end() : end])
        if not text or _DASH_TOKEN.search(text) or _COMPACT_START.search(text):
            return []
        segments.append(_MossSegment(_normalize_speaker_id(label.group("speaker")), text))
    return segments


def _normalize_speaker_id(speaker: str) -> str:
    return f"S{int(speaker[1:]):02d}"


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


__all__ = ["normalize_moss_speaker_aware_transcript"]
