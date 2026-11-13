"""Matching a few words against file names, the way a person names a file they are looking for.

Words are compared without case or accents (``Réunion`` finds ``reunion``),
camelCase and separators split words (``ContratACME_v3`` is ``contrat acme
v3``), and a word may match the start of a name's word (``budg`` finds
``budget``), sit inside one (``acme`` finds ``acmecorp``) or, failing all that,
be a near miss (``contart`` finds ``contrat``).

A rare word weighs more than a common one, as in BM25's IDF: in ``contrat
acme`` among 500 contracts, ``acme`` decides. Term frequency, BM25's other
half, means nothing on a name of a few words, so it isn't used. A word no name
matches at all counts as an average word: it lowers every score without
ruling out the names that match the rest (``signé`` in ``contrat acme signé``),
and the caller is told which words matched nothing.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import get_close_matches

#: Share of the weighted words a name must match, and exceed, to be listed.
MIN_SCORE = 0.5

_STOPWORDS = frozenset(
    {
        *("a", "an", "and", "of", "the", "to", "for", "in", "on", "file", "files", "document", "documents"),
        *("au", "aux", "d", "de", "des", "du", "en", "et", "l", "la", "le", "les", "un", "une"),
        *("fichier", "fichiers"),
    }
)
_CAMEL_CASE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_COMBINING_MARKS = re.compile(r"[\u0300-\u036f]")
_WORD = re.compile(r"[^\W_]+")


def words(text: str) -> list[str]:
    """The words of *text*, lowercased and without accents."""
    plain = _COMBINING_MARKS.sub("", unicodedata.normalize("NFKD", _CAMEL_CASE.sub(" ", text)))
    return _WORD.findall(plain.casefold())


@dataclass(frozen=True, slots=True)
class NameMatch:
    """Candidate *index* matches with *score*, from 0 to 1."""

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class NameRanking:
    """The candidates that match, best first, and the query's words no name matched."""

    matches: list[NameMatch]
    unmatched: list[str]


def rank_by_name(query: str, candidates: Sequence[Sequence[str]]) -> NameRanking:
    """The candidates whose names match *query*; ``candidates[i]`` are candidate *i*'s names."""
    terms = list(dict.fromkeys(words(query)))
    meaningful = [term for term in terms if term not in _STOPWORDS]
    terms = meaningful or terms
    if not terms:
        return NameRanking([], [])

    holders: dict[str, set[int]] = {}
    for index, names in enumerate(candidates):
        for name in names:
            for word in words(name):
                holders.setdefault(word, set()).add(index)
    vocabulary = list(holders)

    weights: list[float] = []
    unmatched: list[str] = []
    scores: dict[int, float] = {}
    for term in terms:
        strengths = _strengths(term, holders, vocabulary)
        if not strengths:
            unmatched.append(term)
            continue
        weight = math.log(1 + len(candidates) / len(strengths))
        weights.append(weight)
        for index, strength in strengths.items():
            scores[index] = scores.get(index, 0.0) + weight * strength
    if not weights:
        return NameRanking([], unmatched)
    total_weight = sum(weights) * (1 + len(unmatched) / len(weights))
    matches = [NameMatch(index, score / total_weight) for index, score in scores.items()]
    return NameRanking(
        sorted((m for m in matches if m.score > MIN_SCORE), key=lambda m: (-m.score, m.index)), unmatched
    )


def _strengths(term: str, holders: dict[str, set[int]], vocabulary: list[str]) -> dict[int, float]:
    """How well each candidate holding a word like *term* matches it: 1 for the word itself, less for a partial match."""
    strengths: dict[int, float] = {}

    def credit(word: str, strength: float) -> None:
        for index in holders[word]:
            if strengths.get(index, 0.0) < strength:
                strengths[index] = strength

    if term in holders:
        credit(term, 1.0)
    if len(term) >= 3:
        for word in vocabulary:
            if word == term:
                continue
            if word.startswith(term):
                credit(word, 0.9)
            elif len(term) >= 4 and term in word:
                credit(word, 0.7)
            elif len(word) >= 4 and word in term:
                credit(word, 0.6)
    if not strengths and len(term) >= 4:
        for word in get_close_matches(term, vocabulary, n=5, cutoff=0.8):
            credit(word, 0.6)
    return strengths


__all__ = ["MIN_SCORE", "NameMatch", "NameRanking", "rank_by_name", "words"]
