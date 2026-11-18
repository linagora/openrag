"""Keep application modules from loading under two import roots."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_layer_imports", Path(__file__).resolve().parents[2] / "scripts/check_layer_imports.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "relative_path",
    [
        "openrag/core/utils/example.py",
        "openrag/app_front.py",
        "openrag/chainlit_api.py",
        "tests/unit/example.py",
        "scripts/example.py",
    ],
)
@pytest.mark.parametrize(
    ("source", "rejected"),
    [
        ("from openrag.core.utils.exceptions import OpenRAGError", True),
        ("import openrag.core.utils.exceptions", True),
        ("from openrag import core", True),
        ("from core.utils.exceptions import OpenRAGError", False),
        ("from .exceptions import OpenRAGError", False),
    ],
)
def test_canonical_import_root(tmp_path, monkeypatch, capsys, relative_path, source, rejected):
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard, "OPENRAG", tmp_path / "openrag")
    guard.OPENRAG.mkdir()
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)

    assert guard.main() == (1 if rejected else 0)
    if rejected:
        assert relative_path in capsys.readouterr().out


@pytest.mark.parametrize(
    "relative_path,rejected",
    [("openrag/core/example.py", True), ("tests/example.py", False), ("scripts/example.py", False)],
)
def test_layer_boundaries_only_apply_to_application_layers(tmp_path, monkeypatch, relative_path, rejected):
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard, "OPENRAG", tmp_path / "openrag")
    guard.OPENRAG.mkdir()
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from services.storage import milvus_store")

    assert guard.main() == (1 if rejected else 0)


@pytest.mark.parametrize(
    ("relative_path", "source", "rejected"),
    [
        # LangGraph only wires the chat workflows.
        ("openrag/services/chat/graphs/classic.py", "from langgraph.graph import StateGraph", False),
        ("openrag/services/orchestrators/chat_service.py", "from langgraph.graph import StateGraph", True),
        ("openrag/services/chat/state.py", "import langgraph.types", True),
        ("openrag/core/chat/state.py", "from langgraph.types import Command", True),
        ("openrag/api/routers/user/chat.py", "import langgraph", True),
        ("tests/unit/services/chat/test_classic.py", "from langgraph.graph import StateGraph", False),
        # The LangGraph Platform client is never loaded, not even by the graph modules.
        ("openrag/services/chat/graphs/classic.py", "import langgraph_sdk", True),
        ("openrag/services/chat/graphs/classic.py", "from langgraph.pregel.remote import RemoteGraph", True),
        ("openrag/services/chat/graphs/classic.py", "from langgraph.pregel import remote", True),
        ("openrag/app_front.py", "from langgraph_sdk.auth import Auth", True),
        # The chat path uses OpenRag's LLM port, not LangChain's model layer.
        ("openrag/services/orchestrators/query_service.py", "from langchain_core.messages import HumanMessage", True),
        ("openrag/services/orchestrators/query_service.py", "from langchain_core import messages", True),
        ("openrag/services/inference/client.py", "import langchain_openai", True),
        ("openrag/services/storage/loader.py", "from langchain_community.document_loaders import PyPDFLoader", True),
        ("openrag/core/models/chunk.py", "from langchain_core.documents.base import Document", False),
        ("openrag/core/utils/text.py", "from langchain_openai import ChatOpenAI", False),
        ("openrag/services/orchestrators/query_service.py", "from langchain_core.documents import Document", False),
    ],
)
def test_third_party_import_rules(tmp_path, monkeypatch, capsys, relative_path, source, rejected):
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard, "OPENRAG", tmp_path / "openrag")
    guard.OPENRAG.mkdir()
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)

    assert guard.main() == (1 if rejected else 0)
    if rejected:
        assert relative_path in capsys.readouterr().out
