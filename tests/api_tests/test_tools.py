"""Tools API tests - extractText and other tools."""

from pathlib import Path

import pytest

RESOURCES_DIR = Path(__file__).parent.parent / "resources"
PDF_FILE = RESOURCES_DIR / "test_file.pdf"
TEXT_FILE = RESOURCES_DIR / "test_file.txt"


@pytest.fixture
def pdf_file_path():
    """Path to the test PDF file."""
    if not PDF_FILE.exists():
        pytest.skip(f"Test PDF not found: {PDF_FILE}")
    return PDF_FILE


@pytest.fixture
def text_file_path():
    """Path to the test text file."""
    if not TEXT_FILE.exists():
        pytest.skip(f"Test text file not found: {TEXT_FILE}")
    return TEXT_FILE


class TestToolsAPI:
    """Test tools endpoint functionality."""

    def test_list_tools(self, api_client):
        """Test listing available tools."""
        response = api_client.get("/v1/tools")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Check extractText tool exists
        tool_names = [t["name"] for t in data]
        assert "extractText" in tool_names

    def test_tool_has_required_fields(self, api_client):
        """Test that tools have required fields."""
        response = api_client.get("/v1/tools")
        data = response.json()

        for tool in data:
            assert "name" in tool
            assert "description" in tool


class TestExtractText:
    """Test extractText tool with various file formats."""

    def test_extract_text_from_pdf(self, api_client, pdf_file_path):
        """Test extracting text from PDF file."""
        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                "/v1/tools/execute",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"tool": '{"name": "extractText"}'},
            )

        assert response.status_code == 200, f"Extract failed: {response.text}"
        result = response.json()

        assert "message" in result
        assert isinstance(result["message"], str)
        assert len(result["message"]) > 0, "Extracted text is empty"

    def test_extract_text_from_text_file(self, api_client, text_file_path):
        """Test extracting text from plain text file."""
        with open(text_file_path, "rb") as f:
            response = api_client.post(
                "/v1/tools/execute",
                files={"file": (text_file_path.name, f, "text/plain")},
                data={"tool": '{"name": "extractText"}'},
            )

        assert response.status_code == 200, f"Extract failed: {response.text}"
        result = response.json()

        assert "message" in result
        assert isinstance(result["message"], str)
        assert len(result["message"]) > 0, "Extracted text is empty"

    def test_extract_text_returns_content(self, api_client, pdf_file_path):
        """Test that extracted text contains meaningful content."""
        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                "/v1/tools/execute",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"tool": '{"name": "extractText"}'},
            )

        result = response.json()
        text = result.get("message", "")

        # Should get substantial text back
        assert len(text) > 10, f"Extracted text too short: {text}"

    def test_extract_text_from_markdown(self, api_client, sample_markdown_file):
        """Test extracting text from markdown file."""
        with open(sample_markdown_file, "rb") as f:
            response = api_client.post(
                "/v1/tools/execute",
                files={"file": ("test.md", f, "text/markdown")},
                data={"tool": '{"name": "extractText"}'},
            )

        assert response.status_code == 200, f"Extract failed: {response.text}"
        result = response.json()

        assert "message" in result
        assert len(result["message"]) > 0, "Extracted text is empty"


class TestExtractTextErrors:
    """Test error handling for extractText tool."""

    def test_extract_text_missing_tool(self, api_client, text_file_path):
        """Test error when tool parameter is missing."""
        with open(text_file_path, "rb") as f:
            response = api_client.post(
                "/v1/tools/execute",
                files={"file": (text_file_path.name, f, "text/plain")},
                # Missing tool parameter
            )

        # Should fail with validation error
        assert response.status_code in [400, 422]

    def test_extract_text_invalid_tool_format(self, api_client, text_file_path):
        """Test error when tool parameter is not valid JSON."""
        with open(text_file_path, "rb") as f:
            response = api_client.post(
                "/v1/tools/execute",
                files={"file": (text_file_path.name, f, "text/plain")},
                data={"tool": "not-valid-json"},
            )

        assert response.status_code == 400
        assert "JSON" in response.text or "json" in response.text

    def test_extract_text_unknown_tool(self, api_client, text_file_path):
        """Test error when requesting unknown tool."""
        with open(text_file_path, "rb") as f:
            response = api_client.post(
                "/v1/tools/execute",
                files={"file": (text_file_path.name, f, "text/plain")},
                data={"tool": '{"name": "unknownTool"}'},
            )

        # Should return error (400 or 500 depending on error handling)
        assert response.status_code in [400, 500]
        assert "not found" in response.text.lower() or "unknownTool" in response.text

    def test_extract_text_missing_file(self, api_client):
        """Test error when file is missing."""
        response = api_client.post(
            "/v1/tools/execute",
            data={"tool": '{"name": "extractText"}'},
            # Missing file
        )

        # Should fail with validation error
        assert response.status_code in [400, 422]
