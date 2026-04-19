"""Azure OpenAI chat-completions client.

Interface-compatible with :class:`autocoder.llm.ollama_client.OllamaClient`
so the orchestrator and heal runner can swap backends at runtime via
``Settings.use_azure_openai``. Specifically, this class exposes the
same three public methods the rest of the codebase relies on:

* ``is_available() -> bool``
* ``chat(system, user, json_mode=True, max_tokens=None, purpose="...") -> LLMResponse``
* ``chat_json(system, user, max_tokens=None, purpose="...", retries=1) -> dict``

The API key is **never** stored on this object. We remember only the
env-var **name** and read the value on every request. Errors are
surfaced as :class:`OllamaError` so existing ``except OllamaError``
handlers keep working regardless of which backend is active.

URL shape (Azure OpenAI data plane):

    POST {endpoint}/openai/deployments/{deployment}/chat/completions
         ?api-version={api_version}
    Headers:
        api-key: {value_from_env}
        Content-Type: application/json

Request body uses the standard Chat Completions schema. We request
``response_format={"type":"json_object"}`` for plan + heal calls so
the backend returns clean JSON without markdown fences.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from autocoder import logger
from autocoder.config import AzureOpenAISettings
from autocoder.llm._json import strict_system_suffix, try_parse_json
from autocoder.llm.ollama_client import LLMResponse, OllamaError


class AzureOpenAIClient:
    def __init__(self, settings: AzureOpenAISettings):
        self._s = settings
        self._client = httpx.Client(
            base_url=settings.endpoint,
            timeout=httpx.Timeout(
                connect=5.0,
                read=settings.timeout_seconds,
                write=10.0,
                pool=5.0,
            ),
        )

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        key = os.environ.get(self._s.api_key_env, "").strip()
        if not key:
            raise OllamaError(
                f"Azure OpenAI is enabled but {self._s.api_key_env} is not "
                f"set. Add it to .env (never commit it)."
            )
        return {"api-key": key, "Content-Type": "application/json"}

    def _url(self) -> str:
        return (
            f"/openai/deployments/{self._s.deployment}"
            f"/chat/completions?api-version={self._s.api_version}"
        )

    def is_available(self) -> bool:
        """Lightweight preflight.

        Azure OpenAI does not have a cheap "list deployments" endpoint
        that uses the data-plane api-key — the control-plane needs a
        different token. So we make a tiny 1-token chat request to
        verify the deployment + key + api-version line up. This costs
        roughly 2 tokens and takes well under a second.
        """
        if not self._s.endpoint:
            logger.warn(
                "azure_openai_endpoint_missing",
                hint="set AZURE_OPENAI_ENDPOINT (or OPENAI_ENDPOINT) in .env",
            )
            return False
        try:
            r = self._client.post(
                self._url(),
                headers=self._headers(),
                json={
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
            )
        except httpx.HTTPError as exc:
            logger.warn(
                "azure_openai_unreachable",
                endpoint=self._s.endpoint,
                err=str(exc),
            )
            return False
        except OllamaError as exc:  # missing api key
            logger.warn("azure_openai_key_missing", err=str(exc))
            return False
        ok = r.status_code == 200
        if ok:
            logger.debug(
                "azure_openai_ready",
                endpoint=self._s.endpoint,
                deployment=self._s.deployment,
                api_version=self._s.api_version,
            )
        else:
            logger.warn(
                "azure_openai_preflight_failed",
                endpoint=self._s.endpoint,
                status=r.status_code,
                body=r.text[:200],
            )
        return ok

    def chat(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        max_tokens: int | None = None,
        purpose: str = "unspecified",
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._s.temperature,
            "top_p": self._s.top_p,
            "max_tokens": max_tokens or self._s.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        logger.debug(
            "azure_openai_request",
            deployment=self._s.deployment,
            purpose=purpose,
            json_mode=json_mode,
            max_tokens=payload["max_tokens"],
            sys_chars=len(system),
            user_chars=len(user),
        )
        start = time.monotonic()
        try:
            r = self._client.post(
                self._url(),
                headers=self._headers(),
                json=payload,
            )
        except httpx.HTTPError as exc:
            logger.error("azure_openai_http_error", purpose=purpose, err=str(exc))
            raise OllamaError(f"Azure OpenAI HTTP error: {exc!s}") from exc
        elapsed = time.monotonic() - start

        if r.status_code != 200:
            logger.error(
                "azure_openai_http_status",
                purpose=purpose,
                status=r.status_code,
                body=r.text[:200],
            )
            raise OllamaError(
                f"Azure OpenAI returned HTTP {r.status_code}: {r.text[:300]}"
            )

        try:
            body = r.json()
        except json.JSONDecodeError as exc:
            logger.error("azure_openai_bad_json_envelope", purpose=purpose, err=str(exc))
            raise OllamaError(f"Azure OpenAI response was not JSON: {exc!s}") from exc

        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("azure_openai_envelope_unexpected", purpose=purpose, keys=list(body)[:8])
            raise OllamaError(
                f"Azure OpenAI response envelope missing choices[0].message.content: {body!r}"
            ) from exc

        usage = body.get("usage") or {}
        in_tokens = int(usage.get("prompt_tokens") or 0)
        out_tokens = int(usage.get("completion_tokens") or 0)
        logger.llm_call(
            model=f"azure:{self._s.deployment}",
            purpose=purpose,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            duration_s=elapsed,
        )
        return LLMResponse(
            text=text,
            eval_count=out_tokens,
            prompt_eval_count=in_tokens,
            duration_seconds=elapsed,
        )

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        purpose: str = "unspecified",
        retries: int = 1,
    ) -> dict[str, Any]:
        """Return a parsed JSON object, retrying once with a stricter prompt.

        Shares the recovery ladder (``try_parse_json``) with the
        Ollama client so failure diagnostics stay uniform across
        backends. Azure's ``response_format={"type":"json_object"}``
        usually makes this a no-op, but the retry layer is cheap and
        covers the occasional edge case (empty string, truncated
        response under ``max_tokens``, content-filter stub).
        """
        attempts: list[str] = []
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            sys_prompt = system
            if attempt > 0:
                sys_prompt = system + strict_system_suffix()
            try:
                resp = self.chat(
                    system=sys_prompt,
                    user=user,
                    json_mode=True,
                    max_tokens=max_tokens,
                    purpose=purpose,
                )
            except OllamaError as exc:
                last_err = exc
                logger.warn(
                    "azure_openai_json_retry_http",
                    purpose=purpose,
                    attempt=attempt,
                    err=str(exc),
                )
                continue

            parsed = try_parse_json(resp.text)
            if parsed is not None:
                if attempt > 0:
                    logger.warn(
                        "azure_openai_json_recovered",
                        purpose=purpose,
                        attempt=attempt,
                        head=resp.text[:80].replace("\n", " "),
                    )
                return parsed

            head = resp.text[:120].replace("\n", " ")
            attempts.append(head)
            logger.warn(
                "azure_openai_json_retry",
                purpose=purpose,
                attempt=attempt,
                head=head,
            )
            last_err = OllamaError(f"unparseable JSON: head={head!r}")

        logger.error(
            "azure_openai_json_parse_failed",
            purpose=purpose,
            attempts=len(attempts),
            err=str(last_err) if last_err else "unknown",
            head=(attempts[-1] if attempts else ""),
        )
        raise OllamaError(
            f"Could not parse JSON from Azure OpenAI after {retries + 1} attempts: "
            f"{last_err!s}"
        )
