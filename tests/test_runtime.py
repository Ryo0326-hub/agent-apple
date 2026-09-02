from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from thetatrap import runtime as runtime_module
from thetatrap.execution import BrokerSnapshot
from thetatrap.runtime import ThetaTrapRuntime
from thetatrap.schedule import ScheduleAction
from thetatrap.settings import load_settings
from thetatrap.storage import Store


class Registry:
    required_schema_hash = "runtime-schema-hash"


def entry_arguments() -> dict[str, Any]:
    return {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "-0.75",
        "client_order_id": "tt-dev-panw-entry-runtime",
        "order_class": "mleg",
        "legs": [
            {"symbol": "PANW260904P00100000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
            {"symbol": "PANW260904P00105000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "PANW260904C00150000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "PANW260904C00155000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
        ],
    }


class RuntimeConnection:
    registry = Registry()

    def __init__(
        self,
        *,
        now: datetime,
        positions: bool = False,
        mismatched_position: bool = False,
        mutation_status: str = "accepted",
    ) -> None:
        self.now = now
        self.with_positions = positions
        self.mismatched_position = mismatched_position
        self.mutation_status = mutation_status
        self.mutations: list[tuple[str, dict[str, Any]]] = []
        self.orders_by_client_id: dict[str, dict[str, Any]] = {}

    async def call_system_read(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_account_info":
            data: Any = {
                "id": "dev-account-id",
                "status": "ACTIVE",
                "equity": "100000",
                "buying_power": "200000",
                "options_trading_level": 3,
            }
        elif name == "get_account_config":
            data = {}
        elif name == "get_clock":
            data = {"is_open": True}
        elif name == "get_orders":
            data = {"result": []}
        elif name == "get_all_positions":
            data = {
                "result": [
                    {
                        "symbol": leg["symbol"],
                        "asset_class": "us_option",
                        "qty": "1",
                        "side": (
                            "long"
                            if leg["side"] == "buy"
                            or (
                                self.mismatched_position
                                and leg["symbol"] == "PANW260904P00105000"
                            )
                            else "short"
                        ),
                    }
                    for leg in entry_arguments()["legs"]
                ]
                if self.with_positions
                else []
            }
        elif name == "get_option_snapshot":
            data = {
                "snapshots": {
                    leg["symbol"]: {
                        "latestQuote": {
                            "bp": "0.20" if leg["side"] == "buy" else "0.60",
                            "ap": "0.25" if leg["side"] == "buy" else "0.70",
                            "t": self.now.isoformat(),
                        }
                    }
                    for leg in entry_arguments()["legs"]
                }
            }
        elif name == "get_account_activities":
            data = {"result": []}
        elif name == "get_order_by_client_id":
            client_order_id = str(arguments["client_order_id"])
            order = self.orders_by_client_id.get(client_order_id)
            if order is None:
                raise RuntimeError("not found")
            data = order
        else:
            raise AssertionError(f"unexpected read {name}")
        return {"_alpaca_mcp_security": {"trust": "untrusted_tool_output"}, "data": data}

    async def call_mutation(self, name: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.mutations.append((name, arguments))
        broker_order_id = f"simulated-order-{len(self.mutations)}"
        client_order_id = str(arguments.get("client_order_id") or "")
        order = {"id": broker_order_id, "status": self.mutation_status}
        if client_order_id:
            order["client_order_id"] = client_order_id
            self.orders_by_client_id[client_order_id] = dict(order)
        return {
            "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
            "data": order,
        }


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "runtime.sqlite3")
    store.initialize()
    return store


@pytest.mark.asyncio
async def test_entry_scan_uses_each_live_collection_timestamp_for_freshness(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_started_at = datetime(2026, 8, 31, 18, 50, tzinfo=UTC)
    collection_finished_at = datetime(2026, 8, 31, 18, 50, 20, tzinfo=UTC)
    settings = load_settings(valid_env_file)
    store = _store(tmp_path)
    connection = RuntimeConnection(now=cycle_started_at)
    runtime = ThetaTrapRuntime(settings, store, connection)  # type: ignore[arg-type]
    event = runtime.events.events[0]
    collection_arguments: dict[str, Any] = {}
    evaluation_arguments: dict[str, Any] = {}

    async def fake_collect(*_: Any, **kwargs: Any) -> Any:
        collection_arguments.update(kwargs)
        return SimpleNamespace(
            symbol=event.symbol,
            collection_id="live-collection",
            collected_at=collection_finished_at,
            source_digest="live-digest",
            previous_trading_day=date(2026, 8, 28),
            underlying=object(),
            front_chain=(),
            back_chain=(),
        )

    def fake_evaluate(**kwargs: Any) -> Any:
        evaluation_arguments.update(kwargs)
        return SimpleNamespace(candidate=None)

    monkeypatch.setattr(runtime_module, "collect_symbol_market", fake_collect)
    monkeypatch.setattr(runtime_module, "evaluate_symbol", fake_evaluate)
    monkeypatch.setattr(runtime, "_persist_collection", lambda *_: None)
    monkeypatch.setattr(runtime, "_persist_evaluation", lambda *_args, **_kwargs: "id")
    snapshot = BrokerSnapshot(
        observed_at=cycle_started_at,
        account={"id": "dev-account-id"},
        account_config={},
        clock={"is_open": True},
        open_orders=(),
        positions=(),
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        options_level=3,
        market_is_open=True,
    )

    result = await runtime._scan_entry(
        snapshot,
        cycle_started_at,
        events_override=(event,),
        strategy_date_override=event.event_date,
    )

    assert result["eligible_candidates"] == 0
    assert "now" not in collection_arguments
    assert collection_arguments["stock_feed"] == "iex"
    assert collection_arguments["option_feed"] == "indicative"
    assert evaluation_arguments["observed_at"] == collection_finished_at
    run = store.find_active_strategy_run(environment=settings.environment)
    assert run is not None
    assert run["context"]["market_data_profile"]["profile_id"] == (
        "alpaca_basic_iex_indicative_v1"
    )


@pytest.mark.asyncio
async def test_closed_weekend_cycle_is_read_only_and_records_heartbeat(
    tmp_path, valid_env_file
) -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    connection = RuntimeConnection(now=now)
    settings = load_settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, connection)  # type: ignore[arg-type]

    report = await runtime.cycle(now=now)

    assert report["status"] == "idle"
    assert report["execution_enabled"] is False
    assert report["market_data_profile"]["profile_id"] == (
        "alpaca_basic_iex_indicative_v1"
    )
    assert connection.mutations == []
    health = store.latest_health()
    assert health is not None
    assert health["status"] == "healthy"
    detail = json.loads(health["detail_json"])
    assert detail["market_data_profile"] == report["market_data_profile"]


@pytest.mark.asyncio
async def test_next_morning_position_builds_and_submits_one_atomic_exit(
    tmp_path, valid_env_file
) -> None:
    now = datetime(2026, 9, 2, 13, 46, tzinfo=UTC)
    connection = RuntimeConnection(now=now, positions=True)
    settings = load_settings(valid_env_file)
    object.__setattr__(settings, "read_only", False)
    object.__setattr__(settings, "execution_enabled", True)
    store = _store(tmp_path)
    store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="hash",
    )
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "POSITION_OPEN"):
        store.transition_strategy_run("run-1", state, "TEST")
    entry = entry_arguments()
    store.record_order_intent(
        "entry-intent",
        run_id="run-1",
        purpose="entry",
        client_order_id=entry["client_order_id"],
        payload=entry,
    )
    runtime = ThetaTrapRuntime(settings, store, connection)  # type: ignore[arg-type]

    report = await runtime.cycle(now=now)

    assert report["status"] == "exit_pending"
    assert len(connection.mutations) == 1
    name, arguments = connection.mutations[0]
    assert name == "place_option_order"
    assert arguments["order_class"] == "mleg"
    assert len(arguments["legs"]) == 4
    assert all(leg["position_intent"].endswith("_to_close") for leg in arguments["legs"])
    assert store.get_strategy_run("run-1")["state"] == "EXIT_PENDING"


@pytest.mark.asyncio
async def test_signed_position_mismatch_enters_risk_off_without_exit_mutation(
    tmp_path, valid_env_file
) -> None:
    now = datetime(2026, 9, 2, 13, 46, tzinfo=UTC)
    connection = RuntimeConnection(
        now=now, positions=True, mismatched_position=True
    )
    settings = load_settings(valid_env_file)
    object.__setattr__(settings, "read_only", False)
    object.__setattr__(settings, "execution_enabled", True)
    store = _store(tmp_path)
    store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="hash",
    )
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "POSITION_OPEN"):
        store.transition_strategy_run("run-1", state, "TEST")
    entry = entry_arguments()
    store.record_order_intent(
        "entry-intent",
        run_id="run-1",
        purpose="entry",
        client_order_id=entry["client_order_id"],
        payload=entry,
    )
    runtime = ThetaTrapRuntime(settings, store, connection)  # type: ignore[arg-type]

    report = await runtime.cycle(now=now)

    assert report["status"] == "risk_off"
    assert report["reason"] == "ASSIGNMENT_OR_UNMATCHED_LEGS"
    assert report["manual_broker_intervention_required"] is True
    assert connection.mutations == []
    assert store.get_strategy_run("run-1")["state"] == "RISK_OFF"
    assert store.get_kill_switch()["kill_switch_enabled"] is True


@pytest.mark.asyncio
async def test_risk_off_working_exit_is_repriced_not_mistaken_for_entry_cancel(
    tmp_path, valid_env_file, monkeypatch
) -> None:
    now = datetime(2026, 9, 2, 13, 54, tzinfo=UTC)
    connection = RuntimeConnection(now=now, positions=True)
    settings = load_settings(valid_env_file)
    object.__setattr__(settings, "read_only", False)
    object.__setattr__(settings, "execution_enabled", True)
    store = _store(tmp_path)
    store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="hash",
    )
    for state in (
        "SCREENING",
        "AI_REVIEW",
        "POLICY_CHECK",
        "SUBMITTING",
        "POSITION_OPEN",
        "EXIT_SUBMITTING",
        "EXIT_PENDING",
        "RISK_OFF",
    ):
        store.transition_strategy_run("run-1", state, "TEST")
    entry = entry_arguments()
    store.record_order_intent(
        "entry-intent",
        run_id="run-1",
        purpose="entry",
        client_order_id=entry["client_order_id"],
        payload=entry,
    )
    exit_arguments = {
        **entry,
        "limit_price": "1.00",
        "client_order_id": "tt-dev-panw-exit-runtime",
        "legs": [
            {
                **leg,
                "side": "sell" if leg["side"] == "buy" else "buy",
                "position_intent": (
                    "sell_to_close" if leg["side"] == "buy" else "buy_to_close"
                ),
            }
            for leg in entry["legs"]
        ],
    }
    store.record_order_intent(
        "exit-intent",
        run_id="run-1",
        purpose="exit",
        client_order_id=exit_arguments["client_order_id"],
        payload=exit_arguments,
    )
    store.create_order_chain(
        "exit-chain", run_id="run-1", intent_id="exit-intent", purpose="exit"
    )
    store.transition_order_chain("exit-chain", "SUBMITTING")
    store.record_order_attempt(
        "exit-attempt",
        chain_id="exit-chain",
        sequence=0,
        client_order_id=exit_arguments["client_order_id"],
        request=exit_arguments,
        broker_order_id="exit-broker-order",
    )
    store.transition_order_chain("exit-chain", "PENDING", attempt_id="exit-attempt")
    runtime = ThetaTrapRuntime(settings, store, connection)  # type: ignore[arg-type]
    cancel_entry = AsyncMock(side_effect=AssertionError("must not cancel entry"))
    reprice_exit = AsyncMock(return_value={"status": "exit_reprice"})
    monkeypatch.setattr(runtime, "_cancel_active_entry", cancel_entry)
    monkeypatch.setattr(runtime, "_reprice_active_order", reprice_exit)
    positions = tuple(
        {
            "symbol": leg["symbol"],
            "asset_class": "us_option",
            "qty": "1",
            "side": "long" if leg["side"] == "buy" else "short",
        }
        for leg in entry["legs"]
    )
    snapshot = BrokerSnapshot(
        observed_at=now,
        account={},
        account_config={},
        clock={"is_open": True},
        open_orders=(
            {
                "id": "exit-broker-order",
                "client_order_id": exit_arguments["client_order_id"],
            },
        ),
        positions=positions,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        options_level=3,
        market_is_open=True,
    )
    run = store.get_strategy_run("run-1")
    assert run is not None

    result = await runtime._manage_active(run, snapshot, ScheduleAction.EXIT, now)

    assert result == {"status": "exit_reprice"}
    cancel_entry.assert_not_awaited()
    reprice_exit.assert_awaited_once_with(run, purpose="exit", now=now)


@pytest.mark.asyncio
async def test_filled_exit_replacement_waits_for_fresh_flat_snapshot(
    tmp_path, valid_env_file
) -> None:
    now = datetime(2026, 9, 2, 13, 54, tzinfo=UTC)
    connection = RuntimeConnection(
        now=now, positions=True, mutation_status="filled"
    )
    settings = load_settings(valid_env_file)
    object.__setattr__(settings, "read_only", False)
    object.__setattr__(settings, "execution_enabled", True)
    store = _store(tmp_path)
    store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="hash",
    )
    for state in (
        "SCREENING",
        "AI_REVIEW",
        "POLICY_CHECK",
        "SUBMITTING",
        "POSITION_OPEN",
        "EXIT_SUBMITTING",
        "EXIT_PENDING",
    ):
        store.transition_strategy_run("run-1", state, "TEST")
    entry = entry_arguments()
    store.record_order_intent(
        "entry-intent",
        run_id="run-1",
        purpose="entry",
        client_order_id=entry["client_order_id"],
        payload=entry,
    )
    exit_arguments = {
        **entry,
        "limit_price": "1.00",
        "client_order_id": "tt-dev-panw-exit-replacement",
        "legs": [
            {
                **leg,
                "side": "sell" if leg["side"] == "buy" else "buy",
                "position_intent": (
                    "sell_to_close" if leg["side"] == "buy" else "buy_to_close"
                ),
            }
            for leg in entry["legs"]
        ],
    }
    store.record_order_intent(
        "exit-intent",
        run_id="run-1",
        purpose="exit",
        client_order_id=exit_arguments["client_order_id"],
        payload=exit_arguments,
    )
    store.create_order_chain(
        "exit-chain", run_id="run-1", intent_id="exit-intent", purpose="exit"
    )
    store.transition_order_chain("exit-chain", "SUBMITTING")
    store.record_order_attempt(
        "exit-attempt",
        chain_id="exit-chain",
        sequence=0,
        client_order_id=exit_arguments["client_order_id"],
        request=exit_arguments,
        broker_order_id="exit-broker-order",
    )
    # Keep the fixture's order age deterministic. ``record_order_attempt`` uses
    # the real wall clock, while this test intentionally drives the runtime with
    # a fixed market timestamp.
    with store.connect() as db_connection:
        db_connection.execute(
            "UPDATE order_attempts SET created_at=? WHERE attempt_id=?",
            ((now - timedelta(seconds=31)).isoformat(), "exit-attempt"),
        )
    store.transition_order_chain(
        "exit-chain", "PENDING", attempt_id="exit-attempt"
    )
    runtime = ThetaTrapRuntime(settings, store, connection)  # type: ignore[arg-type]
    run = store.get_strategy_run("run-1")
    assert run is not None

    replacement = await runtime._reprice_active_order(
        run, purpose="exit", now=now
    )

    assert replacement["status"] == "exit_pending"
    assert replacement["awaiting_position_reconciliation"] is True
    assert store.get_order_chain("exit-chain")["state"] == "FILLED"
    assert store.get_strategy_run("run-1")["state"] == "EXIT_PENDING"

    connection.with_positions = False
    connection.now = datetime(2026, 9, 2, 13, 55, tzinfo=UTC)
    await runtime.cycle(now=connection.now)

    assert store.get_strategy_run("run-1")["state"] == "FLAT"
    assert store.list_strategy_transitions("run-1")[-1]["reason_code"] == (
        "BROKER_FLAT_RECONCILED"
    )
