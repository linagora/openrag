"""MCP server configuration.

Settings for the standalone Model Context Protocol server
(``openrag/api/mcp/server.py``): the FastMCP transport binding plus the
search-tool defaults/bounds applied before a request reaches
``RetrievalService``.
"""

from __future__ import annotations

from .base import ConfigMixin


class MCPServerConfig(ConfigMixin):
    server_name: str = "OpenRAG MCP"
    host: str = "0.0.0.0"
    port: int = 8081
    path: str = "/mcp"
    default_top_k: int = 5
    max_top_k: int = 50
    similarity_threshold: float = 0.8
    # Bounds for the index_url server-side fetch (SSRF/DoS hardening).
    download_timeout: float = 30.0
    max_download_bytes: int = 100 * 1024 * 1024  # 100 MiB
