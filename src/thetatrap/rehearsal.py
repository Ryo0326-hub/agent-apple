"""Ephemeral live-data rehearsal of deterministic screening and Qwen review."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, AsyncContextManager, Callable

from thetatrap.errors import PolicyError
from thetatrap.events import load_events
from thetatrap.mcp.client import MCPConnection, open_alpaca_mcp
from thetatrap.orders import serialize_for_storage
from thetatrap.policy import payload_hash
from thetatrap.runtime import ThetaTrapRuntime
from thetatrap.settings import RuntimeSettings, account_suffix
from thetatrap.storage import Store


ConnectionFactory = Callable[
    [RuntimeSettings, Store], AsyncContextManager[MCPConnection]
]


class MutationProofConnection:
    """Delegate reads and tool discovery while making broker mutation impossible."""

    def __init__(self, delegate: MCPConnection) -> None:
        self._delegate = delegate
        self.registry = delegate.registry
        self.mutation_attempts = 0

    def qwen_openai_tools(self, *, include_entry: bool = False) -> list[dict[str, Any]]:
        return self._delegate.qwen_openai_tools(include_entry=include_entry)

    async def call_system_read(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._delegate.call_system_read(tool_name, arguments)

    async def call_agent_read(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._delegate.call_agent_read(tool_name, arguments)

    async def call_mutation(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.mutation_attempts += 1
        raise PolicyError("decision rehearsal blocks every broker mutation dispatch")


async def run_decision_rehearsal(
    settings: RuntimeSettings,
    strategy_date: date,
    *,
    now: datetime | None = None,
    connection_factory: ConnectionFactory = open_alpaca_mcp,
) -> dict[str, Any]:
    """Run production screening/Qwen logic against live reads in an ephemeral DB."""

    if not settings.read_only or settings.execution_enabled:
        raise PolicyError("decision rehearsal requires the disarmed read-only role")
    settings.require_mcp_credentials()
    settings.require_featherless_credentials()
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    events = load_events()
    requested_events = tuple(
        event
        for event in events.events
        if event.status == "verified" and event.event_date == strategy_date
    )
    if not requested_events:
        raise PolicyError(
            "decision rehearsal date has no verified event in frozen configuration"
        )

    with tempfile.TemporaryDirectory(prefix="thetatrap-decision-rehearsal-") as folder:
        database_path = Path(folder) / "rehearsal.sqlite3"
        rehearsal_settings = settings.model_copy(
            update={"environment": "replay", "database_path": database_path}
        )
        store = Store(database_path)
        store.initialize()
        store.upsert_events(events)

        async with connection_factory(rehearsal_settings, store) as delegate:
            connection = MutationProofConnection(delegate)
            runtime = ThetaTrapRuntime(
                rehearsal_settings,
                store,
                connection,  # type: ignore[arg-type]
                events=events,
            )
            before = await runtime.execution.read_broker_snapshot(now=observed_at)
            result = await runtime.rehearse_entry(
                before,
                observed_at,
                strategy_date=strategy_date,
            )
            after = await runtime.execution.read_broker_snapshot()

            before_orders = _fingerprint(before.open_orders)
            after_orders = _fingerprint(after.open_orders)
            before_positions = _fingerprint(before.positions)
            after_positions = _fingerprint(after.positions)
            broker_unchanged = (
                before_orders == after_orders
                and before_positions == after_positions
                and len(before.open_orders) == len(after.open_orders)
                and len(before.positions) == len(after.positions)
            )
            if connection.mutation_attempts != 0:
                raise PolicyError("decision rehearsal attempted a broker mutation")
            if not broker_unchanged:
                raise PolicyError(
                    "broker orders or positions changed during decision rehearsal"
                )

            run = store.find_strategy_run(
                environment="replay",
                strategy_date=strategy_date.isoformat(),
                strategy_version=events.strategy_version,
            )
            if run is None:
                raise PolicyError("decision rehearsal did not create its ephemeral run")
            candidates = store.list_candidates(run["run_id"])
            agent_runs = _agent_runs(store, run["run_id"])
            tool_trace = _bounded_trace(store, agent_runs)
            decision_status = _decision_status(agent_runs, tool_trace)
            order_attempt_count = _order_attempt_count(store, run["run_id"])
            if order_attempt_count != 0:
                raise PolicyError("decision rehearsal persisted a broker order attempt")

            return {
                "outcome": decision_status,
                "safety_status": "PASS",
                "mode": "EPHEMERAL_LIVE_READ_REHEARSAL",
                "requested_strategy_date": strategy_date.isoformat(),
                "observed_at": observed_at.isoformat(),
                "event_symbols": [event.symbol for event in requested_events],
                "account_suffix": account_suffix(str(before.account["id"])),
                "market_is_open": before.market_is_open,
                "paper_mode": settings.alpaca_paper_trade,
                "execution_enabled": False,
                "ephemeral_database": True,
                "production_database_touched": False,
                "strategy_result": result,
                "strategy_state": (store.get_strategy_run(run["run_id"]) or run)[
                    "state"
                ],
                "deterministic_candidates": [
                    _candidate_summary(store, candidate) for candidate in candidates
                ],
                "qwen_reviews": [_agent_summary(item) for item in agent_runs],
                "bounded_tool_trace": tool_trace,
                "broker_safety": {
                    "mutation_dispatch_attempts": connection.mutation_attempts,
                    "order_attempts_persisted": order_attempt_count,
                    "open_orders_before": len(before.open_orders),
                    "open_orders_after": len(after.open_orders),
                    "positions_before": len(before.positions),
                    "positions_after": len(after.positions),
                    "orders_unchanged": before_orders == after_orders,
                    "positions_unchanged": before_positions == after_positions,
                },
                "required_schema_hash": connection.registry.required_schema_hash,
                "mcp_tool_count": connection.registry.tool_count,
            }


def _candidate_summary(store: Store, candidate: dict[str, Any]) -> dict[str, Any]:
    payload = candidate.get("payload") or {}
    failures = payload.get("failures") if isinstance(payload, dict) else []
    failure_codes = [
        str(item.get("code"))
        for item in failures or []
        if isinstance(item, dict) and item.get("code")
    ]
    gates = store.list_gate_results(str(candidate["candidate_id"]))
    summary: dict[str, Any] = {
        "symbol": candidate["symbol"],
        "eligible": bool(candidate["eligible"]),
        "rank": candidate.get("candidate_rank"),
        "failure_codes": failure_codes,
        "gate_count": len(gates),
    }
    if candidate["eligible"] and isinstance(payload, dict):
        summary["metrics"] = {
            key: payload.get(key)
            for key in (
                "spot",
                "expected_move_fraction",
                "iv_ratio",
                "wing_width",
                "natural_credit",
                "midpoint_credit",
                "proposed_credit",
                "maximum_loss",
                "risk_budget",
                "net_delta",
            )
        }
    return summary


def _agent_runs(store: Store, run_id: str) -> list[dict[str, Any]]:
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM agent_runs WHERE run_id=? ORDER BY started_at, agent_run_id",
            (run_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.pop("result_json", None)
        item["result"] = json.loads(raw) if raw else None
        result.append(item)
    return result


def _agent_summary(agent_run: dict[str, Any]) -> dict[str, Any]:
    result = agent_run.get("result") or {}
    return {
        "status": agent_run["status"],
        "model": result.get("model") or agent_run["model"],
        "outcome": result.get("outcome"),
        "reason_code": result.get("reason_code") or agent_run.get("veto_reason"),
        "turns": result.get("turns"),
        "tool_calls": result.get("tool_calls"),
        "prompt_tokens": result.get("prompt_tokens"),
        "completion_tokens": result.get("completion_tokens"),
    }


def _bounded_trace(
    store: Store, agent_runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for agent_run in agent_runs:
        for call in store.list_agent_tool_calls(str(agent_run["agent_run_id"])):
            name = str(call["tool_name"])
            trace.append(
                {
                    "sequence": len(trace),
                    "tool_name": name,
                    "tool_kind": (
                        "intercepted_order_proposal"
                        if name == "place_option_order"
                        else (
                            "official_mcp_read"
                            if call["is_official_mcp"]
                            else "local_agent_tool"
                        )
                    ),
                    "status": call["status"],
                    "duration_ms": call["duration_ms"],
                    "arguments_hash": payload_hash(call.get("arguments") or {}),
                    "result_hash": payload_hash(call.get("result")),
                    "read_dispatched": bool(
                        call["status"] == "ok"
                        and call["is_official_mcp"]
                        and name != "place_option_order"
                    ),
                    "mutation_dispatched": False,
                }
            )
    return trace


def _decision_status(
    agent_runs: list[dict[str, Any]], tool_trace: list[dict[str, Any]]
) -> str:
    if not agent_runs:
        return "NO_ELIGIBLE_CANDIDATE"
    terminal_decisions = [
        item
        for item in agent_runs
        if item.get("status") in {"COMPLETED", "VETOED"}
        and (item.get("result") or {}).get("outcome")
    ]
    if terminal_decisions and tool_trace:
        return "QWEN_DECISION_RECORDED"
    return "QWEN_REVIEW_FAILED"


def _order_attempt_count(store: Store, run_id: str) -> int:
    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM order_attempts AS attempts
            JOIN order_chains AS chains ON chains.chain_id=attempts.chain_id
            WHERE chains.run_id=?
            """,
            (run_id,),
        ).fetchone()
    return int(row["count"] if row is not None else 0)


def _fingerprint(rows: Any) -> str:
    return payload_hash(serialize_for_storage(rows))
