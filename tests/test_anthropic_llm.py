"""The live model adapter, tested with a fake client: no key, no network."""

import asyncio
from types import SimpleNamespace

import pytest

from payments_agent.agent.anthropic_llm import DEFAULT_MODEL, AnthropicLLM
from payments_agent.agent.types import ToolSpec

TOOLS = [ToolSpec("get_transaction", "Get one", {"type": "object"}, True)]


class FakeBlock:
    def __init__(self, data):
        self.data = data

    def model_dump(self, **_):
        return dict(self.data)


class FakeMessages:
    def __init__(self):
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[
                FakeBlock(
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_transaction",
                        "input": {"transaction_id": "txn_fx_rail"},
                    }
                )
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def fake_client():
    messages = FakeMessages()
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicLLM()


def test_default_model_and_request_shape(monkeypatch):
    monkeypatch.delenv("PAYMENTS_AGENT_MODEL", raising=False)
    client, messages = fake_client()
    llm = AnthropicLLM(client=client)
    response = asyncio.run(
        llm.complete(system="sys", messages=[{"role": "user", "content": "hi"}], tools=TOOLS)
    )
    request = messages.requests[0]
    assert request["model"] == DEFAULT_MODEL == "claude-sonnet-5-5"
    assert request["tools"] == [
        {"name": "get_transaction", "description": "Get one", "input_schema": {"type": "object"}}
    ]
    assert request["fallbacks"] == "default"
    assert response.stop_reason == "tool_use"
    assert response.content[0]["name"] == "get_transaction"
    assert response.usage == {"input_tokens": 10, "output_tokens": 5}


def test_model_override_and_no_fallbacks(monkeypatch):
    monkeypatch.setenv("PAYMENTS_AGENT_MODEL", "claude-opus-5-5")
    client, messages = fake_client()
    llm = AnthropicLLM(client=client, use_fallbacks=False, effort="low")
    asyncio.run(llm.complete(system="s", messages=[], tools=TOOLS))
    request = messages.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert "fallbacks" not in request and "betas" not in request
    assert request["output_config"] == {"effort": "low"}
