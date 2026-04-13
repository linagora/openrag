"""Tests for BaseReranker.rrf_reranking static method."""

from langchain_core.documents.base import Document

from .base import BaseReranker


def make_doc(doc_id: str, content: str = "", **metadata) -> Document:
    return Document(page_content=content, metadata={"_id": doc_id, **metadata})


class TestRrfRerankingSingleList:
    def test_single_list_returned_as_is(self):
        docs = [make_doc("a"), make_doc("b"), make_doc("c")]
        result = BaseReranker.rrf_reranking([docs])
        assert result is docs


class TestRrfRerankingMultipleLists:
    def test_two_lists_no_overlap_all_docs_present(self):
        list1 = [make_doc("a"), make_doc("b")]
        list2 = [make_doc("c"), make_doc("d")]
        result = BaseReranker.rrf_reranking([list1, list2])
        assert {d.metadata["_id"] for d in result} == {"a", "b", "c", "d"}

    def test_document_in_multiple_lists_ranked_higher(self):
        # doc_shared appears rank 1 in both lists
        # doc_only_list1 appears rank 2 in list1 only
        doc_shared = make_doc("shared")
        doc_only = make_doc("only")
        list1 = [doc_shared, doc_only]
        list2 = [doc_shared]
        result = BaseReranker.rrf_reranking([list1, list2])
        ids = [d.metadata["_id"] for d in result]
        assert ids[0] == "shared"

    def test_overlapping_docs_deduped(self):
        doc = make_doc("dup")
        list1 = [doc, make_doc("a")]
        list2 = [doc, make_doc("b")]
        result = BaseReranker.rrf_reranking([list1, list2])
        ids = [d.metadata["_id"] for d in result]
        assert ids.count("dup") == 1

    def test_sorted_descending_by_rrf_score(self):
        # doc_a: rank 1 in both → score = 2/(1+60) ≈ 0.0328
        # doc_b: rank 2 in both → score = 2/(2+60) ≈ 0.0323
        # doc_c: rank 3 in both → score = 2/(3+60) ≈ 0.0317
        doc_a, doc_b, doc_c = make_doc("a"), make_doc("b"), make_doc("c")
        list1 = [doc_a, doc_b, doc_c]
        list2 = [doc_a, doc_b, doc_c]
        result = BaseReranker.rrf_reranking([list1, list2])
        assert [d.metadata["_id"] for d in result] == ["a", "b", "c"]

    def test_higher_rank_in_one_list_can_outweigh_single_appearance(self):
        # doc_top: rank 1 in list1 only → score = 1/61
        # doc_bottom: rank 3 in list1, rank 3 in list2 → score = 2/63
        # 2/63 ≈ 0.0317 > 1/61 ≈ 0.0164, so doc_bottom wins
        doc_top = make_doc("top")
        doc_bottom = make_doc("bottom")
        list1 = [doc_top, make_doc("x"), doc_bottom]
        list2 = [make_doc("y"), make_doc("z"), doc_bottom]
        result = BaseReranker.rrf_reranking([list1, list2])
        ids = [d.metadata["_id"] for d in result]
        assert ids.index("bottom") < ids.index("top")


class TestRrfRerankingDocumentPreservation:
    def test_preserves_page_content_and_metadata(self):
        doc = make_doc("doc1", content="hello world", source="file.pdf", score=0.9)
        result = BaseReranker.rrf_reranking([[doc], [doc]])
        assert result[0].page_content == "hello world"
        assert result[0].metadata["source"] == "file.pdf"
        assert result[0].metadata["score"] == 0.9
