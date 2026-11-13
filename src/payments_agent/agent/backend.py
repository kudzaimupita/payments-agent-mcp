"""Tool backends: the agent always talks MCP, either to an in-process server
(tests and evals) or to a subprocess over stdio (live use)."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from .types import ToolOutcome, ToolSpec


class McpToolBackend:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def list_tools(self) -> list[ToolSpec]:
        listing = await self.client.list_tools()
        specs = []
        for tool in listing.tools:
            read_only = bool(tool.annotations and tool.annotations.read_only_hint)
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema),
                    read_only=read_only,
                )
            )
        return specs

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        result = await self.client.call_tool(name, arguments)
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        payload = result.structured_content
        if payload is None and not result.is_error:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        return ToolOutcome(payload=payload, text=text, is_error=bool(result.is_error))


@asynccontextmanager
async def in_process_backend(server: MCPServer) -> AsyncIterator[McpToolBackend]:
    async with Client(server) as client:
        yield McpToolBackend(client)


@asynccontextmanager
async def stdio_backend(
    env: dict[str, str] | None = None, read_only: bool = False
) -> AsyncIterator[McpToolBackend]:
    args = ["-m", "payments_agent.server"] + (["--read-only"] if read_only else [])
    params = StdioServerParameters(command=sys.executable, args=args, env=env)
    async with Client(params) as client:
        yield McpToolBackend(client)
