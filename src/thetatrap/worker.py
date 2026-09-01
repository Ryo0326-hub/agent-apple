"""Long-lived MCP worker and backwards-compatible read-only smoke cycle."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from thetatrap.agent_smoke import ReadOnlySmokeAgent, SMOKE_SYSTEM_PROMPT
from thetatrap.agent_tools import ReadOnlySmokeTools
from thetatrap.events import load_events
from thetatrap.errors import PolicyError
from thetatrap.mcp.client import MCPConnection, extract_account, open_alpaca_mcp, unwrap_data
from thetatrap.policy import payload_hash
from thetatrap.settings import RuntimeSettings, account_suffix
from thetatrap.storage import Store
from thetatrap.runtime import ThetaTrapRuntime


LOGGER = logging.getLogger(__name__)


def prepare_foundation(
    settings: RuntimeSettings, events_path: str | Path = "config/events.yaml"
) -> Store:
    store = Store(settings.database_path)
    store.initialize()
    store.upsert_events(load_events(events_path))
    return store


async def run_read_cycle(
    settings: RuntimeSettings, store: Store, connection: MCPConnection
) -> dict[str, Any]:
    account_wrapper = await connection.call_read("get_account_info")
    account = extract_account(account_wrapper)
    store.bind_identity(settings.environment, settings.expected_account_id, account["id"])

    # No other broker call occurs until the expected account ID is proven.
    config_wrapper = await connection.call_read("get_account_config")
    clock_wrapper = await connection.call_read("get_clock")
    orders_wrapper = await connection.call_read("get_orders")
    positions_wrapper = await connection.call_read("get_all_positions")

    clock = _dict_data(clock_wrapper)
    orders = unwrap_data(orders_wrapper)
    positions = unwrap_data(positions_wrapper)
    account_config = unwrap_data(config_wrapper)
    market_is_open = _optional_bool(clock.get("is_open"))

    store.record_account_snapshot(account)
    report = {
        "environment": settings.environment,
        "account_suffix": account_suffix(account["id"]),
        "account_status": account.get("status"),
        "paper_mode": settings.alpaca_paper_trade,
        "read_only": settings.read_only,
        "market_is_open": market_is_open,
        "open_order_count": _collection_size(orders),
        "position_count": _collection_size(positions),
        "options_level": _options_level(account, account_config),
        "market_data_profile": settings.market_data_status(),
        "required_schema_hash": connection.registry.required_schema_hash,
        "mcp_tool_count": connection.registry.tool_count,
    }
    store.record_heartbeat(
        status="healthy",
        environment=settings.environment,
        account_suffix=report["account_suffix"],
        mcp_schema_hash=connection.registry.required_schema_hash,
        market_is_open=market_is_open,
        detail=report,
    )
    LOGGER.info("read-only MCP cycle completed", extra={"context": report})
    return report


async def run_mcp_smoke(settings: RuntimeSettings) -> dict[str, Any]:
    """Run the original five-read identity/schema smoke with no strategy actions."""

    settings.require_mcp_credentials()
    store = prepare_foundation(settings)
    async with open_alpaca_mcp(settings, store) as connection:
        return await run_read_cycle(settings, store, connection)


async def run_account_discovery(settings: RuntimeSettings) -> dict[str, Any]:
    """Discover the paper-account UUID without binding or snapshot persistence."""

    if not settings.read_only or settings.execution_enabled:
        raise PolicyError("account discovery requires the disarmed read-only role")
    settings.require_alpaca_credentials()
    store = prepare_foundation(settings)
    async with open_alpaca_mcp(settings, store) as connection:
        account = extract_account(await connection.call_discovery_read())
        account_id = str(account["id"])
        return {
            "environment": settings.environment,
            "paper_mode": settings.alpaca_paper_trade,
            "market_data_profile": settings.market_data_status(),
            "account_id": account_id,
            "account_suffix": account_suffix(account_id),
            "env_assignment": f"THETATRAP_EXPECTED_ACCOUNT_ID={account_id}",
            "required_schema_hash": connection.registry.required_schema_hash,
            "mcp_tool_count": connection.registry.tool_count,
        }


async def run_agent_smoke(settings: RuntimeSettings) -> dict[str, Any]:
    """Prove hosted-model orchestration with five reads and zero mutations."""

    if not settings.read_only or settings.execution_enabled:
        raise PolicyError("agent smoke requires the disarmed read-only role")
    settings.require_mcp_credentials()
    settings.require_featherless_credentials()
    events = load_events()
    store = prepare_foundation(settings)

    async with open_alpaca_mcp(settings, store) as connection:
        account = extract_account(await connection.call_discovery_read())
        account_id = str(account["id"])
        store.bind_identity(
            settings.environment,
            settings.expected_account_id,
            account_id,
        )
        smoke_run_id = "tt-agent-smoke-" + uuid.uuid4().hex
        model_route = " -> ".join(
            (
                settings.featherless_primary_model,
                settings.featherless_fallback_model,
            )
        )
        store.start_agent_smoke(
            smoke_run_id,
            environment=settings.environment,
            account_suffix=account_suffix(account_id),
            model=model_route,
            prompt_hash=payload_hash({"system_prompt": SMOKE_SYSTEM_PROMPT}),
            config_hash=payload_hash(events.model_dump(mode="json")),
        )
        tools = ReadOnlySmokeTools(connection)
        try:
            decision = await ReadOnlySmokeAgent(settings, tools).run()
            if len(decision.trace) != len(tools.calls):
                raise PolicyError("agent smoke trace did not match dispatched read calls")
            for trace_item, executed in zip(decision.trace, tools.calls, strict=True):
                if trace_item.tool_name != executed.name:
                    raise PolicyError("agent smoke trace order did not match dispatch order")
                store.record_agent_smoke_tool(
                    smoke_run_id,
                    executed.sequence,
                    turn=trace_item.turn,
                    tool_name=trace_item.tool_name,
                    arguments_hash=trace_item.arguments_hash,
                    result_hash=trace_item.result_hash,
                    status=executed.status,
                    duration_ms=executed.duration_ms,
                )
            report = {
                "outcome": "PASS",
                "smoke_run_id": smoke_run_id,
                "environment": settings.environment,
                "paper_mode": settings.alpaca_paper_trade,
                "account_suffix": account_suffix(account_id),
                "model": decision.model,
                "turns": decision.turns,
                "tool_calls": decision.tool_calls,
                "prompt_tokens": decision.prompt_tokens,
                "completion_tokens": decision.completion_tokens,
                "read_tools": list(decision.read_tools),
                "readiness": decision.readiness,
                "reasons": list(decision.reasons),
                "mutation_tools_exposed": 0,
                "market_data_profile": settings.market_data_status(),
                "required_schema_hash": connection.registry.required_schema_hash,
                "mcp_tool_count": connection.registry.tool_count,
            }
            store.finish_agent_smoke(smoke_run_id, "COMPLETED", result=report)
            return report
        except Exception as exc:
            store.finish_agent_smoke(
                smoke_run_id,
                "FAILED",
                result={
                    "outcome": "FAIL",
                    "account_suffix": account_suffix(account_id),
                    "market_data_profile": settings.market_data_status(),
                    "completed_reads": [
                        call.name for call in tools.calls if call.status == "ok"
                    ],
                },
                error_type=type(exc).__name__,
            )
            raise


async def run_worker(settings: RuntimeSettings, *, once: bool = False) -> dict[str, Any] | None:
    if once and settings.execution_enabled:
        raise PolicyError(
            "worker --once is prohibited while entry execution is armed; "
            "use the long-lived worker so pending orders and mandatory exits remain managed"
        )
    settings.require_mcp_credentials()
    store = prepare_foundation(settings)
    while True:
        try:
            async with open_alpaca_mcp(settings, store) as connection:
                runtime = ThetaTrapRuntime(settings, store, connection)
                while True:
                    report = await runtime.cycle()
                    if once:
                        return report
                    await asyncio.sleep(settings.worker_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.record_heartbeat(
                status="unhealthy",
                environment=settings.environment,
                account_suffix=None,
                mcp_schema_hash=None,
                market_is_open=None,
                detail={
                    "error_type": type(exc).__name__,
                    "market_data_profile": settings.market_data_status(),
                },
            )
            LOGGER.exception("MCP worker cycle failed")
            if once:
                raise
            await asyncio.sleep(5)


def _dict_data(wrapper: dict[str, Any]) -> dict[str, Any]:
    data = unwrap_data(wrapper)
    return data if isinstance(data, dict) else {}


def _collection_size(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("orders", "positions", "data", "result"):
            nested = value.get(key)
            if isinstance(nested, list):
                return len(nested)
    return None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _options_level(account: Any, account_config: Any) -> Any:
    for payload in (account, account_config):
        if not isinstance(payload, dict):
            continue
        for key in ("options_trading_level", "options_approved_level"):
            if key in payload:
                return payload[key]
    return None
