"""Shared structural types for autocoder LLM clients."""

from __future__ import annotations

from typing import Any, Protocol

from autocoder.llm.ollama_client import LLMResponse


class LLMClient(Protocol):
    def close(self) -> None: ...

    def is_available(self) -> bool: ...

    def availability_for(self, purposes: list[str]) -> bool: ...

    def chat(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        max_tokens: int | None = None,
        purpose: str = "unspecified",
    ) -> LLMResponse: ...

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        purpose: str = "unspecified",
        retries: int = 1,
    ) -> dict[str, Any]: ...
