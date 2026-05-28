from __future__ import annotations

from core.embeddings import embedder_registry
from core.indexing.parsers import parser_registry
from core.llm import llm_registry
from core.rerankers import reranker_registry
from core.vlm import vlm_registry
from di.container import ServiceContainer
from di.inference import register_inference
from di.parsers import register_parsers


class TestRegisterInference:
    def test_registries_populated(self):
        register_inference()

        assert "vllm" in llm_registry
        assert "ollama" in llm_registry
        assert "vllm" in embedder_registry
        assert "ollama" in embedder_registry
        assert "vllm" in vlm_registry
        assert "infinity" in reranker_registry
        assert "openai" in reranker_registry

    def test_idempotent(self):
        register_inference()
        register_inference()


class TestRegisterParsers:
    def test_parser_registry_populated(self):
        register_parsers()

        assert {
            "audio_client",
            "doc",
            "docling",
            "docx",
            "eml",
            "html",
            "image",
            "local_whisper",
            "markdown",
            "marker",
            "pdf_client",
            "pptx",
            "pymupdf",
            "text",
        }.issubset(set(parser_registry.list_registered()))

    def test_idempotent(self):
        register_parsers()
        register_parsers()


class TestServiceContainer:
    def test_container_populates_all_registries(self):
        ServiceContainer()

        assert "vllm" in llm_registry
        assert "ollama" in llm_registry
        assert "vllm" in embedder_registry
        assert "ollama" in embedder_registry
        assert "vllm" in vlm_registry
        assert "infinity" in reranker_registry
        assert "openai" in reranker_registry

    def test_create_llm(self):
        container = ServiceContainer()
        client = container.create_llm(endpoint="http://vllm:8000/v1", model_name="m")
        assert client is not None

    def test_create_embedder(self):
        container = ServiceContainer()
        client = container.create_embedder(endpoint="http://vllm:8000/v1", model_name="m")
        assert client is not None

    def test_create_reranker(self):
        container = ServiceContainer()
        client = container.create_reranker(endpoint="http://reranker:7997", model_name="m")
        assert client is not None

    def test_create_vlm(self):
        container = ServiceContainer()
        client = container.create_vlm(endpoint="http://vllm:8000/v1", model_name="m")
        assert client is not None
