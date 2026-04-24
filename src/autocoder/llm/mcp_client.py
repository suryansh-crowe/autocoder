"""MCP-backed LLM client.

This adapter keeps the rest of autocoder unchanged: planners and heal
flows still call ``chat(...)`` / ``chat_json(...)`` while the actual
provider selection is delegated to the MCP server.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client
from fastmcp.exceptions import ToolError

from autocoder import logger
from autocoder.config import MCPSettings
from autocoder.llm._json import strict_system_suffix, try_parse_json
from autocoder.llm.ollama_client import LLMResponse, OllamaError


def _workload_for_purpose(purpose: str) -> str:
    if purpose.startswith("pom_plan:"):
        return "pom_plan"
    if purpose.startswith("feature_plan:"):
        return "feature_plan"
    if purpose.startswith("heal_fail:"):
        return "heal_fail"
    if purpose.startswith("heal:"):
        return "heal"
    return "general"


def _priority_profile(workload: str) -> dict[str, int]:
    profiles = {
        "pom_plan": {
            "prefer_speed": 4,
            "prefer_intelligence": 5,
            "prefer_privacy": 1,
            "prefer_low_cost": 1,
            "prefer_json_reliability": 5,
        },
        "feature_plan": {
            "prefer_speed": 4,
            "prefer_intelligence": 5,
            "prefer_privacy": 1,
            "prefer_low_cost": 1,
            "prefer_json_reliability": 5,
        },
        "heal": {
            "prefer_speed": 2,
            "prefer_intelligence": 3,
            "prefer_privacy": 4,
            "prefer_low_cost": 4,
            "prefer_json_reliability": 4,
        },
        "heal_fail": {
            "prefer_speed": 3,
            "prefer_intelligence": 4,
            "prefer_privacy": 3,
            "prefer_low_cost": 2,
            "prefer_json_reliability": 4,
        },
        "general": {
            "prefer_speed": 3,
            "prefer_intelligence": 3,
            "prefer_privacy": 2,
            "prefer_low_cost": 2,
            "prefer_json_reliability": 3,
        },
    }
    return dict(profiles.get(workload, profiles["general"]))


class MCPClient:
    def __init__(self, settings: MCPSettings):
        self._s = settings
        self._server_spec = self._build_server_spec()

    def close(self) -> None:
        # Per-call async sessions are opened/closed around each MCP request.
        return None

    def _build_server_spec(self) -> str | dict[str, Any]:
        transport = self._s.transport.lower()
        if transport in {"http", "streamable-http"}:
            return self._s.url
        if transport == "sse":
            return self._s.url[:-4] + "/sse" if self._s.url.endswith("/mcp") else self._s.url
        if transport == "stdio":
            server_cfg: dict[str, Any] = {
                "transport": "stdio",
                "command": self._s.command,
            }
            if self._s.cwd:
                server_cfg["cwd"] = self._s.cwd
            return {"mcpServers": {self._s.server_name: server_cfg}}
        raise OllamaError(f"Unsupported MCP transport: {self._s.transport}")

    async def _call_tool_async(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = Client(self._server_spec)
        async with client:
            result = await client.call_tool(
                tool_name,
                arguments,
                timeout=self._s.timeout_seconds,
            )
        data = result.data if result.data is not None else result.structured_content
        if isinstance(data, dict):
            return data
        raise OllamaError(
            f"MCP tool {tool_name!r} returned non-dict data: {type(data).__name__}"
        )

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            logger.debug(
                "mcp_tool_request",
                tool=tool_name,
                transport=self._s.transport,
                endpoint=self._s.url if self._s.transport != "stdio" else self._s.command,
            )
            return asyncio.run(self._call_tool_async(tool_name, arguments))
        except ToolError as exc:
            raise OllamaError(f"MCP tool {tool_name} failed: {exc!s}") from exc
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"MCP request failed for {tool_name}: {exc!s}") from exc

    def _routing_metadata(
        self,
        *,
        system: str,
        user: str,
        purpose: str,
        json_mode: bool,
        max_tokens: int | None,
        preflight: bool = False,
    ) -> dict[str, Any]:
        workload = _workload_for_purpose(purpose)
        metadata: dict[str, Any] = {
            "workload": workload,
            "json_mode": json_mode,
            "strict_json": json_mode,
            "system_chars": len(system),
            "user_chars": len(user),
            "prompt_chars": len(system) + len(user),
            "max_tokens": int(max_tokens or 0),
            "preflight": preflight,
        }
        metadata.update(_priority_profile(workload))
        return metadata

    def is_available(self) -> bool:
        return self.availability_for([])

    def availability_for(self, purposes: list[str]) -> bool:
        try:
            requests = [
                {
                    "purpose": purpose,
                    "metadata": self._routing_metadata(
                        system="",
                        user="",
                        purpose=purpose,
                        json_mode=True,
                        max_tokens=None,
                        preflight=True,
                    ),
                }
                for purpose in purposes
            ]
            payload = self._call_tool(self._s.llm_ping_tool, {"requests": requests})
        except OllamaError as exc:
            logger.warn("mcp_ping_failed", err=str(exc))
            return False
        ok = bool(payload.get("ok"))
        if ok:
            logger.debug(
                "mcp_ping_ok",
                routes=payload.get("routes", {}),
                route_reasons=payload.get("route_reasons", {}),
                backends=payload.get("backends", {}),
            )
        else:
            logger.warn(
                "mcp_ping_unavailable",
                routes=payload.get("routes", {}),
                route_reasons=payload.get("route_reasons", {}),
                backends=payload.get("backends", {}),
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
        metadata = self._routing_metadata(
            system=system,
            user=user,
            purpose=purpose,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
        payload = self._call_tool(
            self._s.llm_chat_tool,
            {
                "system": system,
                "user": user,
                "purpose": purpose,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
                "metadata": metadata,
            },
        )
        text = str(payload.get("text") or "")
        if not text:
            raise OllamaError("MCP response produced no content")

        in_tokens = int(payload.get("prompt_tokens") or 0)
        out_tokens = int(payload.get("completion_tokens") or 0)
        duration_s = float(payload.get("duration_s") or 0.0)
        logger.llm_call(
            model=str(payload.get("model") or "(unknown)"),
            purpose=purpose,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            duration_s=duration_s,
            backend=str(payload.get("backend") or "mcp"),
            routed_via="mcp",
            route_reason=str(payload.get("reason") or ""),
            failover_used=bool(payload.get("failover_used")),
        )
        return LLMResponse(
            text=text,
            eval_count=out_tokens,
            prompt_eval_count=in_tokens,
            duration_seconds=duration_s,
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
        attempts: list[str] = []
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            sys_prompt = system if attempt == 0 else system + strict_system_suffix()
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
                    "mcp_json_retry_http",
                    purpose=purpose,
                    attempt=attempt,
                    err=str(exc),
                )
                continue

            parsed = try_parse_json(resp.text)
            if parsed is not None:
                if attempt > 0:
                    logger.warn(
                        "mcp_json_recovered",
                        purpose=purpose,
                        attempt=attempt,
                        head=resp.text[:80].replace("\n", " "),
                    )
                return parsed

            head = resp.text[:120].replace("\n", " ")
            attempts.append(head)
            logger.warn(
                "mcp_json_retry",
                purpose=purpose,
                attempt=attempt,
                head=head,
            )
            last_err = OllamaError(f"unparseable JSON: head={head!r}")

        logger.error(
            "mcp_json_parse_failed",
            purpose=purpose,
            attempts=len(attempts),
            err=str(last_err) if last_err else "unknown",
            head=(attempts[-1] if attempts else ""),
        )
        raise OllamaError(
            f"Could not parse JSON from MCP model output after {retries + 1} attempts: "
            f"{last_err!s}"
        )
