from __future__ import annotations

from autocoder.config import load_settings


def test_load_settings_always_uses_mcp_connection(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BACKEND", "azure_openai")
    monkeypatch.setenv("USE_AZURE_OPENAI", "true")
    monkeypatch.setenv("AUTOCODER_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("AUTOCODER_MCP_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("AUTOCODER_MCP_HTTP_PORT", "8765")
    monkeypatch.setenv("AUTOCODER_MCP_HTTP_PATH", "/mcp")

    settings = load_settings(project_root=None)

    assert not hasattr(settings, "llm_backend")
    assert not hasattr(settings, "use_azure_openai")
    assert settings.llm_endpoint() == "http://127.0.0.1:8765/mcp"
    assert "MCP server" in settings.llm_hint()


def test_load_settings_keeps_provider_settings_for_mcp_server(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "phi4:mini")
    monkeypatch.setenv("OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_CHAT_DEPLOYMENT", "gpt-4.1")

    settings = load_settings(project_root=None)

    assert settings.ollama.model == "phi4:mini"
    assert settings.azure_openai.endpoint == "https://example.openai.azure.com"
    assert settings.azure_openai.deployment == "gpt-4.1"
