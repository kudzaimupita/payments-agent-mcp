"""Message and tool types for the agent loop.

Messages use the Anthropic Messages API shape (plain dicts) so the same loop
drives both the real model and the offline scripted model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Block = dict[str, Any]
Message = dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class LLMResponse:
    content: list[Block]
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)


class LLM(Protocol):
    name: str

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse: ...


@dataclass
class ToolOutcome:
    payload: dict[str, Any] | None
    text: str
    is_error: bool


class ToolBackend(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome: ...
