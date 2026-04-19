"""Ollama HTTP client tuned for a CPU-only Phi-4 14B server.

Notes:

* Uses ``httpx`` with a long timeout because Phi-4 14B on CPU is
  slow (~2-4 tok/s, see info/02_local_llm_recommendation.md).
* JSON output mode (``format="json"``) is requested for plan calls so
  the model returns syntactically valid JSON without manual fence
  stripping.
* No streaming — the orchestrator is happier with a single decoded
  JSON object than with a token stream.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from autocoder import logger
from autocoder.config import OllamaSettings
from autocoder.llm._json import strict_system_suffix, try_parse_json


# Back-compat alias so existing unit tests and external call sites
# that imported the private helper keep working.
_try_parse_json = try_parse_json


@dataclass
class LLMResponse:
    text: str
    eval_count: int
    prompt_eval_count: int
    duration_seconds: float


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, settings: OllamaSettings):
        self._s = settings
        self._client = httpx.Client(
            base_url=settings.endpoint,
            timeout=httpx.Timeout(connect=5.0, read=settings.timeout_seconds, write=10.0, pool=5.0),
        )

    def close(self) -> None:
        self._client.close()

    def is_available(self) -> bool:
        try:
            r = self._client.get("/api/tags")
            ok = r.status_code == 200
            if ok:
                logger.debug("ollama_tags_ok", endpoint=self._s.endpoint)
            else:
                logger.warn("ollama_tags_status", endpoint=self._s.endpoint, status=r.status_code)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warn("ollama_tags_unreachable", endpoint=self._s.endpoint, err=str(exc))
            return False

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
            "model": self._s.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "num_ctx": self._s.num_ctx,
                "temperature": self._s.temperature,
                "top_p": self._s.top_p,
                "num_predict": max_tokens or self._s.num_predict,
            },
        }
        if json_mode:
            payload["format"] = "json"

        logger.debug(
            "ollama_request",
            model=self._s.model,
            purpose=purpose,
            json_mode=json_mode,
            num_ctx=self._s.num_ctx,
            num_predict=payload["options"]["num_predict"],
            sys_chars=len(system),
            user_chars=len(user),
        )
        start = time.monotonic()
        try:
            r = self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            logger.error("ollama_http_error", purpose=purpose, err=str(exc))
            raise OllamaError(f"Ollama HTTP error: {exc!s}") from exc
        elapsed = time.monotonic() - start

        if r.status_code != 200:
            logger.error(
                "ollama_http_status",
                purpose=purpose,
                status=r.status_code,
                body=r.text[:200],
            )
            raise OllamaError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")

        try:
            body = r.json()
        except json.JSONDecodeError as exc:
            logger.error("ollama_bad_json_envelope", purpose=purpose, err=str(exc))
            raise OllamaError(f"Ollama response was not JSON: {exc!s}") from exc

        text = (body.get("message") or {}).get("content", "")
        if not text:
            logger.error("ollama_empty_content", purpose=purpose)
            raise OllamaError(f"Ollama response missing message.content: {body!r}")

        in_tokens = int(body.get("prompt_eval_count") or 0)
        out_tokens = int(body.get("eval_count") or 0)
        logger.llm_call(
            model=self._s.model,
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

        Parsing runs through :func:`_try_parse_json`, which tolerates
        markdown fences, prose around the object, and common truncation
        bugs (unterminated strings, missing closing braces). Only when
        all recovery attempts fail do we raise :class:`OllamaError`.
        """
        attempts: list[str] = []
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            sys_prompt = system
            if attempt > 0:
                sys_prompt = (
                    system
                    + "\n\nSTRICT OUTPUT REQUIREMENTS:\n"
                      "- Respond with exactly ONE JSON object and nothing else.\n"
                      "- Do not wrap the response in markdown or prose.\n"
                      "- Close every string and every brace.\n"
                      "- Keep total output short enough to complete."
                )
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
                    "ollama_json_retry_http",
                    purpose=purpose,
                    attempt=attempt,
                    err=str(exc),
                )
                continue

            parsed = _try_parse_json(resp.text)
            if parsed is not None:
                if attempt > 0:
                    logger.warn(
                        "ollama_json_recovered",
                        purpose=purpose,
                        attempt=attempt,
                        head=resp.text[:80].replace("\n", " "),
                    )
                return parsed

            head = resp.text[:120].replace("\n", " ")
            attempts.append(head)
            logger.warn(
                "ollama_json_retry",
                purpose=purpose,
                attempt=attempt,
                head=head,
            )
            last_err = OllamaError(f"unparseable JSON: head={head!r}")

        logger.error(
            "ollama_json_parse_failed",
            purpose=purpose,
            attempts=len(attempts),
            err=str(last_err) if last_err else "unknown",
            head=(attempts[-1] if attempts else ""),
        )
        raise OllamaError(
            f"Could not parse JSON from model output after {retries + 1} attempts: "
            f"{last_err!s}"
        )
