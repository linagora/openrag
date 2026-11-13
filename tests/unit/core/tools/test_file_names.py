"""Unit tests for ``core.tools.file_names``: matching a few words against file names."""

from __future__ import annotations

import pytest
from core.tools.file_names import rank_by_name, words


def _ranked(query: str, names: list[str]) -> list[str]:
    return [names[m.index] for m in rank_by_name(query, [[name] for name in names]).matches]


def test_words_ignore_case_accents_separators_and_camel_case():
    assert words("2024-03_Contrat_ACME_signé_v3.pdf") == ["2024", "03", "contrat", "acme", "signe", "v3", "pdf"]
    assert words("CompteRenduRéunion") == ["compte", "rendu", "reunion"]
    assert words("Отчёт 2026") == ["отчет", "2026"]


def test_every_word_of_the_name_counts_in_any_order():
    names = ["2024-03_Contrat_ACME_signé_v3.pdf", "Facture_ACME.pdf", "Contrat_Orange.pdf"]

    assert _ranked("acme contrat", names)[0] == names[0]


def test_a_rare_word_outweighs_a_common_one():
    names = [f"Contrat_client{i}.pdf" for i in range(50)] + ["Facture_ACME.pdf", "Contrat_ACME.pdf"]

    ranked = _ranked("contrat acme", names)

    assert ranked[:2] == ["Contrat_ACME.pdf", "Facture_ACME.pdf"]
    assert "Contrat_client1.pdf" not in ranked


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("reunion", "Réunion_COPIL.docx"),
        ("budg", "Budget_2026.xlsx"),
        ("acme", "AcmeCorp_offer.pdf"),
        ("contart", "Contrat_Orange.pdf"),
    ],
)
def test_a_word_can_match_partly_or_with_a_typo(query, name):
    assert _ranked(query, [name, "Unrelated_file.txt"]) == [name]


def test_words_no_name_has_are_reported_and_lower_the_scores():
    names = ["Contrat_ACME.pdf", "Contrat_Orange.pdf", "Facture_Orange.pdf"]

    ranking = rank_by_name("contrat acme signé", [[name] for name in names])

    assert ranking.unmatched == ["signe"]
    assert [names[m.index] for m in ranking.matches] == ["Contrat_ACME.pdf"]
    assert ranking.matches[0].score < 1


def test_half_the_words_is_not_a_match():
    # "zzyzx" counts as an average word: the contract matches half the query.
    ranking = rank_by_name("contrat zzyzx", [["Contrat_ACME.pdf"], ["Facture.pdf"]])

    assert ranking.unmatched == ["zzyzx"]
    assert ranking.matches == []


def test_two_equally_rare_words_must_both_match():
    names = ["Contrat_ACME.pdf", "Facture_ACME.pdf", "Contrat_Orange.pdf"]

    assert _ranked("contrat acme", names) == ["Contrat_ACME.pdf"]


def test_stopwords_are_ignored_unless_they_are_all_there_is():
    names = ["Budget_2026.xlsx", "Le_document.pdf"]

    assert _ranked("le fichier du budget", names) == ["Budget_2026.xlsx"]
    assert _ranked("le document", names) == ["Le_document.pdf"]


def test_nothing_to_match():
    assert rank_by_name("", [["a.pdf"]]).matches == []
    assert rank_by_name("acme", []).matches == []
