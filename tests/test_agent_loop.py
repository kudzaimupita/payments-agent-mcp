import asyncio
import json

from payments_agent.agent.backend import in_process_backend
from payments_agent.agent.loop import AgentConfig, run_agent
from payments_agent.agent.types import LLMResponse
from payments_agent.server import build_server


class LoopingLLM:
    """Always asks for another tool call."""

    name = "test:looping"

    async def complete(self, *, system, messages, tools):
        n = len(messages)
        return LLMResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": f"t{n}",
                    "name": "get_transaction",
                    "input": {"transaction_id": "txn_fx_rail"},
                }
            ],
            stop_reason="tool_use",
        )


class ScriptLLM:
    """Plays back a fixed list of responses."""

    name = "test:script"

    def __init__(self, responses):
        self.responses = list(responses)

    async def complete(self, *, system, messages, tools):
        return self.responses.pop(0)


def tool(name, args, i=1):
    return LLMResponse(
        content=[{"type": "tool_use", "id": f"t{i}", "name": name, "input": args}],
        stop_reason="tool_use",
    )


def text(t):
    return LLMResponse(content=[{"type": "text", "text": t}], stop_reason="end_turn")


def run(service, llm, config=None, message="hello"):
    async def go():
        async with in_process_backend(build_server(service)) as backend:
            return await run_agent(message, llm=llm, backend=backend, config=config)

    return asyncio.run(go())


def test_max_steps_limit(service):
    result = run(service, LoopingLLM(), AgentConfig(max_steps=3))
    assert result.status == "max_steps"
    assert len(result.tool_calls) == 3
    assert "step limit" in result.final_answer


def test_unknown_tool_is_blocked(service):
    result = run(service, ScriptLLM([tool("approve", {"approval_id": "apr_123456"}), text("ok")]))
    assert result.tool_calls[0].blocked == "unknown_tool"


def test_write_budget(service):
    calls = [
        tool(
            "issue_refund",
            {
                "transaction_id": "txn_fx_rail",
                "amount_minor": 100,
                "reason": "budget test",
                "idempotency_key": f"budget-{i:04d}",
            },
            i,
        )
        for i in range(3)
    ]
    result = run(service, ScriptLLM([*calls, text("done")]), AgentConfig(max_write_calls=2))
    assert [c.blocked for c in result.tool_calls] == [None, None, "write_budget"]
    assert service.conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 2


def test_final_answer_is_redacted(service):
    result = run(service, ScriptLLM([text("Email ada.okafor@example.com or 07700 900101")]))
    assert result.answer_pii == ["email", "phone"]
    assert "ada.okafor" not in result.final_answer
    assert "[REDACTED_EMAIL]" in result.final_answer


def test_transcript_log_is_redacted(service, tmp_path):
    log = tmp_path / "t.jsonl"
    run(
        service,
        ScriptLLM([tool("get_transaction", {"transaction_id": "txn_fx_rail"}), text("done")]),
        AgentConfig(log_path=log),
        message="customer ada.okafor@example.com asked about it",
    )
    content = log.read_text()
    assert "ada.okafor@example.com" not in content
    assert [json.loads(line)["event"] for line in content.splitlines()] == [
        "start",
        "assistant",
        "tool",
        "assistant",
        "end",
    ]


def test_refusal_stops_the_loop(service):
    result = run(service, ScriptLLM([LLMResponse(content=[], stop_reason="refusal")]))
    assert result.status == "refused"
    assert result.tool_calls == []


def test_tool_results_are_wrapped_as_untrusted(service):
    seen = {}

    class Capture(ScriptLLM):
        async def complete(self, *, system, messages, tools):
            if len(messages) > 1:
                seen["result"] = messages[-1]["content"][0]["content"]
            return await super().complete(system=system, messages=messages, tools=tools)

    run(
        service, Capture([tool("get_transaction", {"transaction_id": "txn_fx_rail"}), text("done")])
    )
    assert seen["result"].startswith('<tool_result tool="get_transaction" trust="untrusted-data">')
