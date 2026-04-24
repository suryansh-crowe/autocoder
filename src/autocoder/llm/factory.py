"""Return the MCP-backed LLM client for the current run.

Autocoder no longer selects Ollama vs Azure OpenAI directly. Every LLM
request goes through the sibling MCP server, which inspects request
metadata and chooses the concrete provider on the server side.
"""

from __future__ import annotations

from autocoder import logger
from autocoder.config import Settings
from autocoder.llm.protocols import LLMClient


def get_llm_client(settings: Settings) -> LLMClient:
    """Return the MCP transport client used by planners and heal flows."""
    from autocoder.llm.mcp_client import MCPClient

    logger.info(
        "llm_backend_selected",
        backend="mcp",
        transport=settings.mcp.transport,
        endpoint=settings.mcp.url if settings.mcp.transport != "stdio" else settings.mcp.command,
        chat_tool=settings.mcp.llm_chat_tool,
        ping_tool=settings.mcp.llm_ping_tool,
    )
    return MCPClient(settings.mcp)
