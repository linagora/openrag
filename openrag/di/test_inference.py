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
    """Verify inference implementation registration side effects."""

    def test_registries_populated(self):
        """Register all supported inference backends in core registries."""
        register_inference()

        assert "vllm" in llm_registry
        assert "ollama" in llm_registry
        assert "vllm" in embedder_registry
        assert "ollama" in embedder_registry
        assert "vllm" in vlm_registry
        assert "infinity" in reranker_registry
        assert "openai" in reranker_registry

    def test_idempotent(self):
        """Allow repeated inference registration without duplicate failures."""
        register_inference()
        register_inference()


class TestRegisterParsers:
    """Verify parser implementation registration side effects."""

    def test_parser_registry_populated(self):
        """Register all supported parser implementations in the core registry."""
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
        """Allow repeated parser registration without duplicate failures."""
        register_parsers()
        register_parsers()


class TestServiceContainer:
    """Verify service container registry initialization and factories."""

    def test_container_populates_all_registries(self):
        """Populate inference registries when a container is constructed."""
        ServiceContainer()

        assert "vllm" in llm_registry
        assert "ollama" in llm_registry
        assert "vllm" in embedder_registry
        assert "ollama" in embedder_registry
        assert "vllm" in vlm_registry
        assert "infinity" in reranker_registry
        assert "openai" in reranker_registry

    def test_create_llm(self):
        """Create an LLM client through the container factory."""
        container = ServiceContainer()
        client = container.create_llm(endpoint="http://vllm:8000/v1", model_name="m")
        assert client is not None

    def test_create_embedder(self):
        """Create an embedder client through the container factory."""
        container = ServiceContainer()
        client = container.create_embedder(endpoint="http://vllm:8000/v1", model_name="m")
        assert client is not None

    def test_create_reranker(self):
        """Create a reranker client through the container factory."""
        container = ServiceContainer()
        client = container.create_reranker(endpoint="http://reranker:7997", model_name="m")
        assert client is not None

    def test_create_vlm(self):
        """Create a VLM client through the container factory."""
        container = ServiceContainer()
        client = container.create_vlm(endpoint="http://vllm:8000/v1", model_name="m")
        assert client is not None
