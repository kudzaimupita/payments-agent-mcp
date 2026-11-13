import asyncio

from payments_agent.agent.backend import in_process_backend, stdio_backend
from payments_agent.server import READ_TOOLS, WRITE_TOOLS, build_server


def _tools(service, **kwargs):
    async def go():
        async with in_process_backend(build_server(service, **kwargs)) as backend:
            return await backend.list_tools()

    return {t.name: t for t in asyncio.run(go())}


def test_tool_tiers(service):
    tools = _tools(service)
    assert set(tools) == set(READ_TOOLS) | set(WRITE_TOOLS)
    assert all(tools[name].read_only for name in READ_TOOLS)
    assert not any(tools[name].read_only for name in WRITE_TOOLS)


def test_approve_is_never_exposed(service):
    tools = _tools(service)
    assert not any("approve" in name and name != "list_pending_approvals" for name in tools)


def test_read_only_mode_hides_write_tools(service):
    assert set(_tools(service, read_only=True)) == set(READ_TOOLS)


def test_refund_schema_requires_idempotency_key(service):
    schema = _tools(service)["issue_refund"].input_schema
    assert set(schema["required"]) == {
        "transaction_id",
        "amount_minor",
        "reason",
        "idempotency_key",
    }
    assert schema["properties"]["idempotency_key"]["minLength"] == 8


def test_invalid_arguments_are_tool_errors(service):
    async def go():
        async with in_process_backend(build_server(service)) as backend:
            bad_id = await backend.call("get_transaction", {"transaction_id": "DROP TABLE"})
            negative = await backend.call(
                "issue_refund",
                {
                    "transaction_id": "txn_fx_rail",
                    "amount_minor": -5,
                    "reason": "x y z",
                    "idempotency_key": "abcdefgh",
                },
            )
            business = await backend.call(
                "issue_refund",
                {
                    "transaction_id": "txn_fx_decl_51",
                    "amount_minor": 5,
                    "reason": "x y z",
                    "idempotency_key": "abcdefgh",
                },
            )
            return bad_id, negative, business

    bad_id, negative, business = asyncio.run(go())
    assert bad_id.is_error and negative.is_error
    assert not business.is_error
    assert business.payload["error"]["code"] == "REFUND_NOT_ALLOWED"


def test_stdio_transport_end_to_end(settings):
    async def go():
        env = {"PAYMENTS_HOME": str(settings.home)}
        async with stdio_backend(env=env) as backend:
            names = {t.name for t in await backend.list_tools()}
            result = await backend.call(
                "issue_refund",
                {
                    "transaction_id": "txn_fx_coffee_ok",
                    "amount_minor": 100,
                    "reason": "stdio test",
                    "idempotency_key": "stdio-0001",
                },
            )
            return names, result

    names, result = asyncio.run(go())
    assert "issue_refund" in names
    assert result.payload["status"] == "pending_approval"
