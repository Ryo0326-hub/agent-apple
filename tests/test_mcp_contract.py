from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from thetatrap.errors import MCPContractError
from thetatrap.mcp.contract import (
    MUTATION_TOOLS,
    QWEN_ENTRY_TOOL,
    QWEN_READ_TOOLS,
    REQUIRED_TOOLS,
    ToolRegistry,
    validate_native_mleg,
)


class FakeTool:
    def __init__(self, name: str, properties: dict[str, Any] | None = None):
        self.name = name
        self.properties = properties or {}

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": f"Tool {self.name}",
            "inputSchema": {
                "type": "object",
                "properties": self.properties,
                "additionalProperties": False,
            },
            "outputSchema": None,
            "annotations": None,
        }


def registry() -> ToolRegistry:
    return ToolRegistry.from_tools([FakeTool(name) for name in sorted(REQUIRED_TOOLS)])


def test_required_schema_hash_is_stable() -> None:
    first = registry()
    second = ToolRegistry.from_tools(
        [FakeTool(name) for name in sorted(REQUIRED_TOOLS, reverse=True)]
    )
    assert first.required_schema_hash == second.required_schema_hash


def test_missing_required_tool_fails_closed() -> None:
    tools = [FakeTool(name) for name in sorted(REQUIRED_TOOLS - {"place_option_order"})]
    with pytest.raises(MCPContractError, match="place_option_order"):
        ToolRegistry.from_tools(tools)


def test_unknown_arguments_are_rejected() -> None:
    with pytest.raises(MCPContractError, match="unknown arguments"):
        registry().validate_arguments("get_account_info", {"symbol": "SPY"})


def test_mleg_must_be_native_four_object_array() -> None:
    with pytest.raises(MCPContractError, match="native array"):
        validate_native_mleg({"qty": "1", "order_class": "mleg", "legs": "[]"})
    validate_native_mleg(
        {
            "qty": "1",
            "order_class": "mleg",
            "legs": [
                {
                    "symbol": f"AAPL260904P00{strike}000",
                    "ratio_qty": "1",
                    "side": "buy",
                    "position_intent": "buy_to_open",
                }
                for strike in (190, 195, 200, 205)
            ],
        }
    )


def test_qwen_has_one_mutation_only() -> None:
    assert QWEN_ENTRY_TOOL == "place_option_order"
    assert not (QWEN_READ_TOOLS & MUTATION_TOOLS)
    assert "place_stock_order" not in MUTATION_TOOLS


def test_committed_contract_lists_exact_required_tools() -> None:
    manifest = json.loads(
        Path("config/alpaca_mcp_contract.v2.3.0.json").read_text(encoding="utf-8")
    )
    assert set(manifest["required_tool_names"]) == REQUIRED_TOOLS
    assert len(manifest["required_schema_hash"]) == 64
