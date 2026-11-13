"""Claude via the Anthropic Python SDK.

Only constructed when running live; tests and offline evals never import a
key or make a network call. Server-side refusal fallbacks are enabled by
default and can be switched off.
"""

from __future__ import annotations

import os
from typing import Any

from .types import LLMResponse, Message, ToolSpec

DEFAULT_MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    def __init__(
        self,
        model: str | None = None,
        *,
        client: Any | None = None,
        max_tokens: int = 16000,
        effort: str | None = None,
        use_fallbacks: bool = True,
    ) -> None:
        self.model = model or os.environ.get("PAYMENTS_AGENT_MODEL", DEFAULT_MODEL)
        self.name = f"anthropic:{self.model}"
        self.max_tokens = max_tokens
        self.effort = effort
        self.use_fallbacks = use_fallbacks
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set; use --llm rules for offline runs")
            import anthropic

            client = anthropic.AsyncAnthropic()
        self.client = client

    def build_request(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "tools": [t.to_anthropic() for t in tools],
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        if self.use_fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        request = self.build_request(system=system, messages=messages, tools=tools)
        if self.use_fallbacks:
            response = await self.client.beta.messages.create(**request)
        else:
            response = await self.client.messages.create(**request)
        content = [block.model_dump(mode="json", exclude_none=True) for block in response.content]
        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
        }
        return LLMResponse(
            content=content, stop_reason=response.stop_reason or "end_turn", usage=usage
        )
