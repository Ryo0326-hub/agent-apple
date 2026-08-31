"""MCP discovery normalization, hashing, and input validation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jsonschema import validators

from thetatrap.errors import MCPContractError


QWEN_READ_TOOLS = frozenset(
    {
        "get_account_info",
        "get_account_config",
        "get_clock",
        "get_orders",
        "get_all_positions",
        "get_news",
    }
)
SETUP_READ_TOOLS = frozenset(
    {
        "get_account_info",
        "get_account_config",
        "get_clock",
        "get_orders",
        "get_all_positions",
    }
)
SYSTEM_READ_TOOLS = QWEN_READ_TOOLS | frozenset(
    {
        "get_account_activities",
        "get_calendar",
        "get_option_contracts",
        "get_stock_latest_quote",
        "get_option_chain",
        "get_option_latest_quote",
        "get_option_snapshot",
        "get_open_position",
        "get_order_by_id",
        "get_order_by_client_id",
    }
)
QWEN_ENTRY_TOOL = "place_option_order"
AGENT_MUTATION_TOOLS = frozenset({QWEN_ENTRY_TOOL})
SYSTEM_MUTATION_TOOLS = frozenset(
    {
        "place_option_order",
        "replace_order_by_id",
        "cancel_order_by_id",
    }
)
MUTATION_TOOLS = AGENT_MUTATION_TOOLS | SYSTEM_MUTATION_TOOLS
FORBIDDEN_MUTATION_TOOLS = frozenset(
    {
        "place_stock_order",
        "place_crypto_order",
        "cancel_all_orders",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "do_not_exercise_options_position",
        "update_account_config",
    }
)
SYSTEM_REQUIRED_TOOLS = (SYSTEM_READ_TOOLS - QWEN_READ_TOOLS) | SYSTEM_MUTATION_TOOLS
REQUIRED_TOOLS = SYSTEM_READ_TOOLS | MUTATION_TOOLS
QWEN_EXPOSED_TOOLS = QWEN_READ_TOOLS | AGENT_MUTATION_TOOLS

_MLEG_LEG_KEYS = frozenset({"symbol", "ratio_qty", "side", "position_intent"})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any] | None

    @classmethod
    def from_mcp_tool(cls, tool: Any) -> "ToolSpec":
        payload = tool.model_dump(mode="json", by_alias=True)
        return cls(
            name=str(payload["name"]),
            description=str(payload.get("description") or ""),
            input_schema=dict(payload.get("inputSchema") or {}),
            output_schema=dict(payload["outputSchema"])
            if payload.get("outputSchema")
            else None,
            annotations=dict(payload["annotations"]) if payload.get("annotations") else None,
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations,
        }


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]):
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise MCPContractError("MCP returned duplicate tool names")
        missing = sorted(REQUIRED_TOOLS - self._specs.keys())
        if missing:
            raise MCPContractError("required MCP tools are missing: " + ", ".join(missing))

    @classmethod
    def from_tools(cls, tools: list[Any]) -> "ToolRegistry":
        return cls([ToolSpec.from_mcp_tool(tool) for tool in tools])

    @property
    def tool_count(self) -> int:
        return len(self._specs)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def get(self, tool_name: str) -> ToolSpec:
        try:
            return self._specs[tool_name]
        except KeyError as exc:
            raise MCPContractError(f"tool was not discovered: {tool_name}") from exc

    def qwen_openai_tools(self, *, include_entry: bool = False) -> list[dict[str, Any]]:
        """Convert only the model-approved discovered tools to OpenAI format.

        The mutation schema is omitted until an immutable entry intent is pending.
        No generic converter is exposed because the discovered MCP server also
        contains broad liquidation, exercise, configuration, and crypto tools.
        """

        names = set(QWEN_READ_TOOLS)
        if include_entry:
            names.add(QWEN_ENTRY_TOOL)
        return [self._as_openai_tool(name) for name in sorted(names)]

    def smoke_openai_tools(self) -> list[dict[str, Any]]:
        """Expose only the five setup reads to the read-only model smoke test."""

        return [self._as_openai_tool(name) for name in sorted(SETUP_READ_TOOLS)]

    def _as_openai_tool(self, tool_name: str) -> dict[str, Any]:
        if tool_name not in QWEN_EXPOSED_TOOLS:
            raise MCPContractError(f"tool is not approved for Qwen exposure: {tool_name}")
        spec = self.get(tool_name)
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": deepcopy(spec.input_schema),
            },
        }

    @property
    def required_schema_hash(self) -> str:
        payload = [self._specs[name].canonical() for name in sorted(REQUIRED_TOOLS)]
        return _sha256(payload)

    @property
    def full_schema_hash(self) -> str:
        payload = [self._specs[name].canonical() for name in sorted(self._specs)]
        return _sha256(payload)

    def snapshot(self) -> dict[str, Any]:
        return {
            "required_schema_hash": self.required_schema_hash,
            "full_schema_hash": self.full_schema_hash,
            "required_tools": [self._specs[name].canonical() for name in sorted(REQUIRED_TOOLS)],
            "all_tool_names": sorted(self._specs),
        }

    def validate_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise MCPContractError(f"arguments for {tool_name} must be an object")
        schema = self.get(tool_name).input_schema
        properties = schema.get("properties", {})
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise MCPContractError(
                f"unknown arguments for {tool_name}: " + ", ".join(unknown)
            )
        validator_class = validators.validator_for(schema)
        validator_class.check_schema(schema)
        errors = sorted(
            validator_class(schema).iter_errors(arguments),
            key=lambda item: tuple(str(part) for part in item.path),
        )
        if errors:
            raise MCPContractError(f"invalid arguments for {tool_name}: {errors[0].message}")

    def assert_expected_hash(self, expected: str | None) -> None:
        if expected and self.required_schema_hash != expected:
            raise MCPContractError(
                "required MCP schema hash mismatch: "
                f"expected {expected}, observed {self.required_schema_hash}"
            )


def validate_native_mleg(arguments: dict[str, Any]) -> None:
    if arguments.get("order_class") != "mleg":
        raise MCPContractError("place_option_order requires order_class=mleg")
    if not isinstance(arguments.get("qty"), str):
        raise MCPContractError("place_option_order qty must be a string")
    legs = arguments.get("legs")
    if not isinstance(legs, list):
        raise MCPContractError("place_option_order legs must be a native array")
    if len(legs) != 4 or not all(isinstance(leg, dict) for leg in legs):
        raise MCPContractError("ThetaTrap MLEG order requires exactly four leg objects")
    for leg in legs:
        if set(leg) != _MLEG_LEG_KEYS:
            raise MCPContractError(
                "each place_option_order leg requires only symbol, ratio_qty, side, "
                "and position_intent"
            )
        if not all(isinstance(leg[key], str) for key in _MLEG_LEG_KEYS):
            raise MCPContractError("place_option_order leg values must be strings")


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
