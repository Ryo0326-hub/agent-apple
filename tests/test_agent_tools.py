from __future__ import annotations

from typing import Any

import pytest

from thetatrap.agent import AgentContext
from thetatrap.agent_tools import ReadOnlySmokeTools, RuntimeAgentTools
from thetatrap.errors import PolicyError
from thetatrap.mcp.contract import SETUP_READ_TOOLS


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def qwen_openai_tools(self, *, include_entry: bool = False) -> list[dict[str, Any]]:
        names = [
            "get_account_info",
            "get_account_config",
            "get_clock",
            "get_orders",
            "get_all_positions",
            "get_news",
        ]
        if include_entry:
            names.append("place_option_order")
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object"},
                },
            }
            for name in names
        ]

    async def call_agent_read(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {
            "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
            "data": {"tool": name},
        }

    def smoke_openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object"},
                },
            }
            for name in sorted(SETUP_READ_TOOLS)
        ]

    async def call_agent_smoke_read(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        data: Any = {"tool": name}
        if name == "get_account_info":
            data = {
                "id": "full-account-uuid-never-send",
                "account_number": "PA-NEVER-SEND",
                "status": "ACTIVE",
                "options_trading_level": 3,
            }
        elif name == "get_orders":
            data = [{"id": "order-never-send"}]
        return {
            "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
            "data": data,
        }


def _context() -> AgentContext:
    return AgentContext(
        symbol="PANW",
        event={"symbol": "PANW"},
        candidate={"maximum_loss": "425.00"},
        run_summary={"state": "AI_REVIEW"},
        order_intent_id="intent-1",
        order_arguments={"qty": "1", "legs": []},
    )


def _tools() -> tuple[RuntimeAgentTools, FakeConnection]:
    connection = FakeConnection()
    tools = RuntimeAgentTools(
        connection,  # type: ignore[arg-type]
        _context(),
        verified_events=[{"symbol": "PANW", "status": "verified"}],
    )
    return tools, connection


def test_definitions_narrow_news_orders_and_exact_mutation() -> None:
    tools, _ = _tools()
    definitions = {
        item["function"]["name"]: item["function"] for item in tools.definitions()
    }
    assert definitions["get_news"]["parameters"]["properties"]["symbols"]["const"] == "PANW"
    assert definitions["get_orders"]["parameters"]["required"] == [
        "status",
        "asset_class",
        "nested",
        "limit",
    ]
    assert definitions["place_option_order"]["parameters"]["properties"]["qty"] == {
        "const": "1"
    }


@pytest.mark.asyncio
async def test_reads_only_dispatch_bounded_official_queries() -> None:
    tools, connection = _tools()
    await tools.execute("get_account_info", {})
    await tools.execute(
        "get_orders",
        {"status": "open", "asset_class": ["us_option"], "nested": True, "limit": 100},
    )
    await tools.execute(
        "get_news",
        {"symbols": "PANW", "include_content": False, "limit": 5, "sort": "desc"},
    )
    assert [item[0] for item in connection.calls] == [
        "get_account_info",
        "get_orders",
        "get_news",
    ]
    assert all(call.is_official_mcp for call in tools.calls)


@pytest.mark.asyncio
async def test_cross_symbol_news_and_direct_mutation_are_rejected() -> None:
    tools, connection = _tools()
    with pytest.raises(PolicyError, match="candidate-symbol"):
        await tools.execute(
            "get_news",
            {"symbols": "AAPL", "include_content": False, "limit": 20, "sort": "desc"},
        )
    with pytest.raises(PolicyError, match="intercepted"):
        await tools.execute("place_option_order", {"qty": "1"})
    assert connection.calls == []


@pytest.mark.asyncio
async def test_local_candidate_and_rejection_are_bound_to_run() -> None:
    tools, _ = _tools()
    candidate = await tools.execute("get_candidate", {})
    assert candidate["symbol"] == "PANW"
    result = await tools.execute(
        "record_candidate_rejection",
        {
            "reason_code": "TRADING_HALT",
            "explanation": "A current halt is reported.",
            "evidence": ["official feed item"],
        },
    )
    assert result == {"recorded": True, "reason_code": "TRADING_HALT"}
    assert tools.rejection is not None
    assert tools.calls[-1].is_official_mcp is False


def test_read_only_smoke_exposes_only_five_fixed_read_schemas() -> None:
    connection = FakeConnection()
    tools = ReadOnlySmokeTools(connection)  # type: ignore[arg-type]
    definitions = {
        item["function"]["name"]: item["function"]
        for item in tools.definitions()
    }

    assert set(definitions) == SETUP_READ_TOOLS
    assert definitions["get_orders"]["parameters"]["required"] == [
        "status",
        "nested",
        "limit",
    ]
    assert definitions["get_orders"]["parameters"]["properties"]["limit"] == {
        "type": "integer",
        "const": 100,
    }


@pytest.mark.asyncio
async def test_smoke_tool_results_remove_account_and_order_identifiers() -> None:
    connection = FakeConnection()
    tools = ReadOnlySmokeTools(connection)  # type: ignore[arg-type]

    account = await tools.execute("get_account_info", {})
    orders = await tools.execute(
        "get_orders", {"status": "open", "nested": True, "limit": 100}
    )

    assert account["data"] == {
        "observed": True,
        "status": "ACTIVE",
        "options_trading_level": 3,
    }
    assert orders["data"] == {"observed": True, "item_count": 1}
    assert "full-account-uuid" not in str(account)
    assert "order-never-send" not in str(orders)
    with pytest.raises(PolicyError, match="not approved"):
        await tools.execute("place_option_order", {})
    with pytest.raises(PolicyError, match="already called"):
        await tools.execute("get_account_info", {})

    fresh = ReadOnlySmokeTools(FakeConnection())  # type: ignore[arg-type]
    with pytest.raises(PolicyError, match="fixed bounds"):
        await fresh.execute(
            "get_orders", {"status": "open", "nested": 1, "limit": 100}
        )
