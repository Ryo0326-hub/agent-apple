"""Run-scoped tool surface exposed to the Featherless Qwen reviewer.

The adapter deliberately narrows the discovered Alpaca MCP schemas.  Qwen can
read only the six evidence sources required by policy, while the one broker
mutation is returned to the host as a proposed action and is never dispatched
from this class.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from thetatrap.agent import AgentContext, LOCAL_TOOL_NAMES, VETO_CODES, local_tool_definitions
from thetatrap.errors import PolicyError
from thetatrap.mcp.client import MCPConnection
from thetatrap.mcp.contract import SETUP_READ_TOOLS


@dataclass(frozen=True, slots=True)
class ExecutedAgentTool:
    sequence: int
    name: str
    arguments: dict[str, Any]
    result: Any
    status: str
    duration_ms: int
    is_official_mcp: bool


class ReadOnlySmokeTools:
    """Expose exactly five fixed Alpaca reads and return summary-only evidence."""

    def __init__(self, connection: MCPConnection) -> None:
        self.connection = connection
        self.calls: list[ExecutedAgentTool] = []
        self._completed: set[str] = set()

    def definitions(self) -> list[dict[str, Any]]:
        definitions = self.connection.smoke_openai_tools()
        for definition in definitions:
            function = definition["function"]
            name = function["name"]
            if name == "get_orders":
                function["parameters"] = {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "const": "open"},
                        "nested": {"type": "boolean", "const": True},
                        "limit": {"type": "integer", "const": 100},
                    },
                    "required": ["status", "nested", "limit"],
                    "additionalProperties": False,
                }
            else:
                function["parameters"] = _empty_schema()
        return definitions

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        started = time.monotonic()
        result: Any = None
        status = "error"
        try:
            if name not in SETUP_READ_TOOLS:
                raise PolicyError(f"agent smoke tool is not approved: {name}")
            if name in self._completed:
                raise PolicyError(f"agent smoke tool was already called: {name}")
            if name == "get_orders":
                if (
                    set(arguments) != {"status", "nested", "limit"}
                    or arguments.get("status") != "open"
                    or arguments.get("nested") is not True
                    or isinstance(arguments.get("limit"), bool)
                    or arguments.get("limit") != 100
                ):
                    raise PolicyError("agent smoke orders query must use its fixed bounds")
            else:
                _require_exact_keys(arguments, set(), name)
            wrapper = await self.connection.call_agent_smoke_read(name, arguments)
            result = _smoke_model_view(name, wrapper)
            self._completed.add(name)
            status = "ok"
            return result
        finally:
            self.calls.append(
                ExecutedAgentTool(
                    sequence=len(self.calls),
                    name=name,
                    arguments=deepcopy(arguments),
                    result=deepcopy(result),
                    status=status,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    is_official_mcp=True,
                )
            )


class RuntimeAgentTools:
    """Concrete, candidate-bound implementation of the Qwen tool protocol."""

    def __init__(
        self,
        connection: MCPConnection,
        context: AgentContext,
        *,
        verified_events: list[dict[str, Any]],
    ) -> None:
        self.connection = connection
        self.context = context
        self.verified_events = deepcopy(verified_events)
        self.calls: list[ExecutedAgentTool] = []
        self.rejection: dict[str, Any] | None = None

    def definitions(self) -> list[dict[str, Any]]:
        official = self.connection.qwen_openai_tools(include_entry=True)
        for definition in official:
            function = definition["function"]
            name = function["name"]
            if name in {
                "get_account_info",
                "get_account_config",
                "get_clock",
                "get_all_positions",
            }:
                function["parameters"] = _empty_schema()
            elif name == "get_orders":
                function["parameters"] = {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "const": "open"},
                        "asset_class": {
                            "type": "array",
                            "prefixItems": [{"type": "string", "const": "us_option"}],
                            "minItems": 1,
                            "maxItems": 1,
                        },
                        "nested": {"type": "boolean", "const": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["status", "asset_class", "nested", "limit"],
                    "additionalProperties": False,
                }
            elif name == "get_news":
                function["parameters"] = {
                    "type": "object",
                    "properties": {
                        "symbols": {"type": "string", "const": self.context.symbol},
                        "include_content": {"type": "boolean", "const": False},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        "sort": {"type": "string", "const": "desc"},
                    },
                    "required": ["symbols", "include_content", "limit", "sort"],
                    "additionalProperties": False,
                }
            elif name == "place_option_order":
                # The host still performs a canonical hash comparison.  These
                # const schemas make the intended call easier for the model and
                # reduce accidental argument drift.
                function["parameters"] = _const_object_schema(
                    self.context.order_arguments
                )
        return official + local_tool_definitions()

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        started = time.monotonic()
        is_official = name not in LOCAL_TOOL_NAMES
        result: Any = None
        status = "error"
        try:
            if name == "place_option_order":
                raise PolicyError(
                    "entry proposals are intercepted by the host policy gateway"
                )
            if name in {
                "get_account_info",
                "get_account_config",
                "get_clock",
                "get_all_positions",
            }:
                _require_exact_keys(arguments, set(), name)
                result = await self.connection.call_agent_read(name, {})
            elif name == "get_orders":
                if (
                    set(arguments) != {"status", "asset_class", "nested", "limit"}
                    or arguments.get("status") != "open"
                    or arguments.get("asset_class") != ["us_option"]
                    or arguments.get("nested") is not True
                    or isinstance(arguments.get("limit"), bool)
                    or not isinstance(arguments.get("limit"), int)
                    or not 1 <= arguments["limit"] <= 100
                ):
                    raise PolicyError("get_orders must use the bounded open-option query")
                result = await self.connection.call_agent_read(name, arguments)
            elif name == "get_news":
                if (
                    set(arguments) != {"symbols", "include_content", "limit", "sort"}
                    or arguments.get("symbols") != self.context.symbol
                    or arguments.get("include_content") is not False
                    or arguments.get("sort") != "desc"
                    or isinstance(arguments.get("limit"), bool)
                    or not isinstance(arguments.get("limit"), int)
                    or not 1 <= arguments["limit"] <= 20
                ):
                    raise PolicyError("get_news must use the bounded candidate-symbol query")
                result = await self.connection.call_agent_read(name, arguments)
            elif name == "list_verified_events":
                _require_exact_keys(arguments, set(), name)
                result = {"events": deepcopy(self.verified_events)}
            elif name == "get_candidate":
                _require_exact_keys(arguments, set(), name)
                result = {
                    "symbol": self.context.symbol,
                    "candidate": deepcopy(self.context.candidate),
                    "immutable_order_intent": {
                        "intent_id": self.context.order_intent_id,
                        "arguments": deepcopy(self.context.order_arguments),
                    },
                }
            elif name == "get_run_summary":
                _require_exact_keys(arguments, set(), name)
                result = deepcopy(self.context.run_summary)
            elif name == "record_candidate_rejection":
                self.rejection = _validated_rejection(arguments)
                result = {"recorded": True, "reason_code": self.rejection["reason_code"]}
            else:
                raise PolicyError(f"agent tool is not approved: {name}")
            status = "ok"
            return result
        finally:
            self.calls.append(
                ExecutedAgentTool(
                    sequence=len(self.calls),
                    name=name,
                    arguments=deepcopy(arguments),
                    result=deepcopy(result),
                    status=status,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    is_official_mcp=is_official,
                )
            )


def _empty_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _const_object_schema(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            key: {"const": deepcopy(item)} for key, item in value.items()
        },
        "required": sorted(value),
        "additionalProperties": False,
    }


def _require_exact_keys(arguments: dict[str, Any], keys: set[str], name: str) -> None:
    if set(arguments) != keys:
        raise PolicyError(f"{name} received unexpected arguments")


def _validated_rejection(arguments: dict[str, Any]) -> dict[str, Any]:
    if set(arguments) != {"reason_code", "explanation", "evidence"}:
        raise PolicyError("candidate rejection must contain the exact approved fields")
    reason = arguments.get("reason_code")
    explanation = arguments.get("explanation")
    evidence = arguments.get("evidence")
    if reason not in VETO_CODES:
        raise PolicyError("candidate rejection reason is not allowlisted")
    if not isinstance(explanation, str) or not (1 <= len(explanation) <= 800):
        raise PolicyError("candidate rejection explanation is invalid")
    if (
        not isinstance(evidence, list)
        or len(evidence) > 5
        or not all(isinstance(item, str) and len(item) <= 500 for item in evidence)
    ):
        raise PolicyError("candidate rejection evidence is invalid")
    return deepcopy(arguments)


def _smoke_model_view(name: str, wrapper: dict[str, Any]) -> dict[str, Any]:
    """Minimize broker data before returning it to the hosted model."""

    data = wrapper.get("data")
    summary: dict[str, Any] = {"observed": True}
    if name == "get_account_info" and isinstance(data, dict):
        for key in ("status", "options_trading_level", "options_approved_level"):
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)):
                summary[key] = value
    elif name == "get_account_config" and isinstance(data, dict):
        for key in ("suspend_trade", "no_shorting"):
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)):
                summary[key] = value
    elif name == "get_clock" and isinstance(data, dict):
        for key in ("is_open", "timestamp", "next_open", "next_close"):
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)):
                summary[key] = value
    elif name in {"get_orders", "get_all_positions"}:
        count = _collection_size(data, "orders" if name == "get_orders" else "positions")
        summary["item_count"] = count
    return {
        "_alpaca_mcp_security": {
            "trust": "untrusted_tool_output",
            "tool_name": name,
        },
        "data": summary,
    }


def _collection_size(value: Any, collection_key: str) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        nested = value.get(collection_key)
        if isinstance(nested, list):
            return len(nested)
        for key in ("data", "result"):
            nested = value.get(key)
            if isinstance(nested, list):
                return len(nested)
    return None
