"""Pick the right LLM backend for the current run.

Called once by the orchestrator and once by the heal runner. Honours
``Settings.use_azure_openai`` to route every ``chat_json`` call to
either the local Ollama server or an Azure OpenAI deployment, with
identical logging, JSON recovery, and error surfacing.

Both backends expose the same three methods the rest of the code
uses: ``is_available() -> bool``, ``chat(...)``, ``chat_json(...)``.
We type the factory's return as ``Any`` instead of a Protocol to
keep the change surface small — nothing downstream uses ``isinstance``
checks on the client.
"""

from __future__ import annotations

from typing import Any

from autocoder import logger
from autocoder.config import Settings
from autocoder.llm.azure_client import AzureOpenAIClient
from autocoder.llm.ollama_client import OllamaClient


def get_llm_client(settings: Settings) -> Any:
    """Return a ready-to-use LLM client for this run.

    Selection:

    * ``USE_AZURE_OPENAI=true`` (via ``Settings.use_azure_openai``)
      -> :class:`AzureOpenAIClient` against the deployment declared
      in ``AzureOpenAISettings``.
    * anything else -> :class:`OllamaClient` against the local
      endpoint declared in ``OllamaSettings``.

    The selection is logged once at startup so runs stay auditable.
    """
    if settings.use_azure_openai:
        logger.info(
            "llm_backend_selected",
            backend="azure_openai",
            endpoint=settings.azure_openai.endpoint,
            deployment=settings.azure_openai.deployment,
            api_version=settings.azure_openai.api_version,
            api_key_present=bool(
                settings.secret_present(settings.azure_openai.api_key_env)
            ),
        )
        return AzureOpenAIClient(settings.azure_openai)
    logger.info(
        "llm_backend_selected",
        backend="ollama",
        endpoint=settings.ollama.endpoint,
        model=settings.ollama.model,
    )
    return OllamaClient(settings.ollama)
