"""Principal-scoped official Alpaca MCP stdio lifecycle and policy gateway."""

from __future__ import annotations

import importlib.metadata
import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from thetatrap.errors import MCPContractError, MCPToolError
from thetatrap.mcp.contract import (
    AGENT_MUTATION_TOOLS,
    QWEN_READ_TOOLS,
    SETUP_READ_TOOLS,
    SYSTEM_MUTATION_TOOLS,
    SYSTEM_READ_TOOLS,
    ToolRegistry,
    validate_native_mleg,
)
from thetatrap.policy import (
    MutationPermit,
    MutationPurpose,
    validate_mleg_arguments,
)
from thetatrap.settings import RuntimeSettings, account_suffix
from thetatrap.storage import Store


ALPACA_MCP_DISTRIBUTION = "alpaca-mcp-server"
ALPACA_MCP_VERSION = "2.3.0"
READ_TIMEOUT_SECONDS = 20
MUTATION_TIMEOUT_SECONDS = 45

Principal = Literal["agent", "system"]


class MCPConnection:
    def __init__(
        self,
        *,
        session: ClientSession,
        registry: ToolRegistry,
        session_id: str,
        store: Store,
        initialization: Any,
    ):
        self.session = session
        self.registry = registry
        self.session_id = session_id
        self.store = store
        self.initialization = initialization

    def qwen_openai_tools(self, *, include_entry: bool = False) -> list[dict[str, Any]]:
        return self.registry.qwen_openai_tools(include_entry=include_entry)

    def smoke_openai_tools(self) -> list[dict[str, Any]]:
        return self.registry.smoke_openai_tools()

    async def call_agent_read(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if tool_name not in QWEN_READ_TOOLS:
            raise MCPContractError(f"agent principal denies MCP read tool: {tool_name}")
        args = _arguments(arguments, tool_name)
        self.registry.validate_arguments(tool_name, args)
        return await self._dispatch(
            tool_name,
            args,
            principal="agent",
            timeout_seconds=READ_TIMEOUT_SECONDS,
        )

    async def call_system_read(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if tool_name not in SYSTEM_READ_TOOLS:
            raise MCPContractError(f"system principal denies MCP read tool: {tool_name}")
        args = _arguments(arguments, tool_name)
        self.registry.validate_arguments(tool_name, args)
        return await self._dispatch(
            tool_name,
            args,
            principal="system",
            timeout_seconds=READ_TIMEOUT_SECONDS,
        )

    async def call_discovery_read(self) -> dict[str, Any]:
        """Read the paper-account identity while persisting only a redacted digest."""

        tool_name = "get_account_info"
        self.registry.validate_arguments(tool_name, {})
        return await self._dispatch(
            tool_name,
            {},
            principal="system",
            timeout_seconds=READ_TIMEOUT_SECONDS,
            audit_profile="discovery",
        )

    async def call_agent_smoke_read(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Dispatch one fixed smoke-test read with summary-only persistence."""

        if tool_name not in SETUP_READ_TOOLS:
            raise MCPContractError(f"agent smoke denies MCP tool: {tool_name}")
        args = _arguments(arguments, tool_name)
        self.registry.validate_arguments(tool_name, args)
        return await self._dispatch(
            tool_name,
            args,
            principal="agent",
            timeout_seconds=READ_TIMEOUT_SECONDS,
            audit_profile="smoke",
        )

    async def call_read(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        principal: str = "system",
    ) -> dict[str, Any]:
        """Backward-compatible narrow read API used by the setup checkpoint."""

        if principal != "system" or tool_name not in SETUP_READ_TOOLS:
            raise MCPContractError(f"Checkpoint 1 denies MCP tool call: {tool_name}")
        return await self.call_system_read(tool_name, arguments)

    async def call_mutation(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        permit: MutationPermit,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        if type(permit) is not MutationPermit:
            raise MCPContractError("MCP mutations require an exact MutationPermit")
        if principal is None:
            principal = permit.principal
        if principal == "agent":
            allowed = AGENT_MUTATION_TOOLS
            if permit.purpose is not MutationPurpose.ENTRY:
                raise MCPContractError("agent mutation permit must authorize entry")
        elif principal == "system":
            allowed = SYSTEM_MUTATION_TOOLS
            if permit.purpose is MutationPurpose.ENTRY:
                raise MCPContractError("system principal cannot use an entry permit")
        else:
            raise MCPContractError(f"unknown MCP mutation principal: {principal}")
        if tool_name not in allowed:
            raise MCPContractError(
                f"{principal} principal denies MCP mutation tool: {tool_name}"
            )

        args = _arguments(arguments, tool_name)
        self.registry.validate_arguments(tool_name, args)

        if tool_name == "place_option_order":
            validate_native_mleg(args)
            validate_mleg_arguments(
                args,
                action="entry" if principal == "agent" else "exit",
            )
        permit.assert_call(
            tool_name=tool_name,
            principal=principal,
            arguments=args,
        )

        return await self._dispatch(
            tool_name,
            args,
            principal=principal,
            timeout_seconds=MUTATION_TIMEOUT_SECONDS,
        )

    async def _dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        principal: Principal,
        timeout_seconds: int,
        audit_profile: Literal["full", "discovery", "smoke"] = "full",
    ) -> dict[str, Any]:
        started = time.monotonic()
        serialized: dict[str, Any] | None = None
        try:
            result = await self.session.call_tool(
                tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            )
            primitive = _to_primitive(result)
            if not isinstance(primitive, dict):
                raise MCPToolError(f"MCP tool {tool_name} returned a non-object result")
            serialized = primitive
            if bool(serialized.get("isError") or serialized.get("is_error")):
                raise MCPToolError(f"MCP tool {tool_name} returned isError")
            wrapper = _structured_wrapper(serialized, tool_name)
            data = wrapper.get("data")
            if isinstance(data, dict) and "error" in data:
                raise MCPToolError(f"MCP tool {tool_name} returned an error payload")
            self.store.record_mcp_call(
                self.session_id,
                principal,
                tool_name,
                arguments,
                _audit_dispatch_payload(tool_name, wrapper, audit_profile),
                "ok",
                round((time.monotonic() - started) * 1000),
            )
            return wrapper
        except Exception as exc:
            self.store.record_mcp_call(
                self.session_id,
                principal,
                tool_name,
                arguments,
                _audit_dispatch_payload(
                    tool_name,
                    serialized or {"error_type": type(exc).__name__},
                    audit_profile,
                ),
                "error",
                round((time.monotonic() - started) * 1000),
            )
            raise


@asynccontextmanager
async def open_alpaca_mcp(
    settings: RuntimeSettings, store: Store
) -> AsyncIterator[MCPConnection]:
    installed_version = importlib.metadata.version(ALPACA_MCP_DISTRIBUTION)
    if installed_version != ALPACA_MCP_VERSION:
        raise MCPContractError(
            f"expected {ALPACA_MCP_DISTRIBUTION} {ALPACA_MCP_VERSION}, "
            f"found {installed_version}"
        )
    executable = _server_executable(settings)
    if not executable.is_file():
        raise MCPContractError(f"Alpaca MCP executable not found: {executable}")

    parameters = StdioServerParameters(
        command=str(executable),
        args=["--transport", "stdio"],
        env=_child_environment(settings),
        encoding="utf-8",
        encoding_error_handler="strict",
    )
    session_id = str(uuid.uuid4())
    started_in_store = False
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=READ_TIMEOUT_SECONDS),
            ) as session:
                initialization = await session.initialize()
                tools = await _list_all_tools(session)
                registry = ToolRegistry.from_tools(tools)
                registry.assert_expected_hash(settings.mcp_expected_schema_hash)
                server_name = _server_name(initialization)
                store.start_mcp_session(
                    session_id,
                    installed_version,
                    server_name,
                    registry.tool_count,
                    registry.required_schema_hash,
                )
                started_in_store = True
                yield MCPConnection(
                    session=session,
                    registry=registry,
                    session_id=session_id,
                    store=store,
                    initialization=initialization,
                )
        if started_in_store:
            store.finish_mcp_session(session_id, "closed")
    except Exception as exc:
        if started_in_store:
            store.finish_mcp_session(session_id, "error", type(exc).__name__)
        raise


async def _list_all_tools(session: ClientSession) -> list[Any]:
    tools: list[Any] = []
    cursor: str | None = None
    while True:
        page = (
            await session.list_tools()
            if cursor is None
            else await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
        )
        tools.extend(page.tools)
        cursor = page.nextCursor
        if cursor is None:
            return tools


def unwrap_data(wrapper: dict[str, Any]) -> Any:
    return wrapper.get("data")


def extract_account(account_wrapper: dict[str, Any]) -> dict[str, Any]:
    data = unwrap_data(account_wrapper)
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise MCPToolError("get_account_info did not return a string account id")
    return data


def _server_executable(settings: RuntimeSettings) -> Path:
    if settings.mcp_server_command:
        return Path(settings.mcp_server_command).expanduser().resolve()
    return Path(sys.executable).with_name("alpaca-mcp-server")


def _child_environment(settings: RuntimeSettings) -> dict[str, str]:
    child: dict[str, str] = {
        "ALPACA_API_KEY": settings.alpaca_api_key.get_secret_value(),
        "ALPACA_SECRET_KEY": settings.alpaca_secret_key.get_secret_value(),
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": settings.alpaca_toolsets,
        "PYTHONUNBUFFERED": "1",
    }
    for key in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(key)
        if value:
            child[key] = value
    return child


def _server_name(initialization: Any) -> str:
    payload = _to_primitive(initialization)
    server_info = payload.get("serverInfo") or payload.get("server_info") or {}
    return str(server_info.get("name") or "unknown")


def _arguments(arguments: dict[str, Any] | None, tool_name: str) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise MCPContractError(f"arguments for {tool_name} must be an object")
    return arguments


def _structured_wrapper(
    serialized_result: dict[str, Any], tool_name: str
) -> dict[str, Any]:
    wrapper = serialized_result.get("structuredContent") or serialized_result.get(
        "structured_content"
    )
    if not isinstance(wrapper, dict):
        raise MCPToolError("MCP result did not include structuredContent")
    security = wrapper.get("_alpaca_mcp_security")
    if not isinstance(security, dict):
        raise MCPToolError(f"MCP tool {tool_name} omitted Alpaca security metadata")
    if security.get("trust") != "untrusted_tool_output":
        raise MCPToolError(f"MCP tool {tool_name} returned invalid trust metadata")
    if security.get("tool_name") != tool_name:
        raise MCPToolError(f"MCP tool {tool_name} returned mismatched security metadata")
    expected_risk = "external_text" if tool_name == "get_news" else "api_structured"
    if security.get("risk") != expected_risk:
        raise MCPToolError(f"MCP tool {tool_name} returned invalid output-risk metadata")
    instructions = security.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise MCPToolError(f"MCP tool {tool_name} omitted trust-boundary instructions")
    if "data" not in wrapper:
        raise MCPToolError(f"MCP tool {tool_name} omitted wrapped data")
    return wrapper


def _to_primitive(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {str(key): _to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_primitive(item) for item in value]
    return value


def _audit_dispatch_payload(
    tool_name: str,
    payload: Any,
    profile: Literal["full", "discovery", "smoke"],
) -> Any:
    """Return a persistence-safe representation of one MCP result.

    Normal runtime calls retain the existing full audit contract. Account
    discovery and the model smoke test are deliberately summary-only: the
    caller still receives the live response in memory, while SQLite receives
    only shape metadata, a one-way payload digest, and a redacted account
    suffix where useful.
    """

    if profile == "full":
        return payload

    primitive = _to_primitive(payload)
    encoded = json.dumps(
        primitive,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    data = _audit_data(primitive)
    summary: dict[str, Any] = {
        "audit_profile": profile,
        "tool_name": tool_name,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_kind": type(data).__name__,
    }
    item_count = _audit_item_count(data)
    if item_count is not None:
        summary["item_count"] = item_count
    if tool_name == "get_account_info" and isinstance(data, dict):
        account_id = data.get("id")
        if isinstance(account_id, str):
            summary["account_suffix"] = account_suffix(account_id)
        for key in (
            "status",
            "options_trading_level",
            "options_approved_level",
        ):
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)):
                summary[key] = value
    return summary


def _audit_data(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    wrapper = payload.get("structuredContent") or payload.get("structured_content")
    if isinstance(wrapper, dict):
        return wrapper.get("data")
    if "data" in payload:
        return payload.get("data")
    return payload


def _audit_item_count(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("orders", "positions", "data", "result"):
            nested = value.get(key)
            if isinstance(nested, list):
                return len(nested)
    return None
