from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from thetatrap.errors import PolicyError
from thetatrap.execution import ExecutionService, quote_atomic_exit
from thetatrap.settings import load_settings
from thetatrap.storage import Store


NOW = datetime(2026, 9, 1, 19, 0, tzinfo=UTC)


def entry_arguments() -> dict[str, Any]:
    return {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "-0.75",
        "client_order_id": "tt-dev-panw-entry-a1b2c3",
        "order_class": "mleg",
        "legs": [
            {"symbol": "PANW260904P00100000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
            {"symbol": "PANW260904P00105000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "PANW260904C00150000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "PANW260904C00155000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
        ],
    }


def exit_arguments() -> dict[str, Any]:
    entry = entry_arguments()
    return {
        **entry,
        "limit_price": "1.00",
        "client_order_id": "tt-dev-panw-exit-a1b2c3",
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


def entry_positions() -> list[dict[str, Any]]:
    return [
        {
            "symbol": leg["symbol"],
            "asset_class": "us_option",
            "qty": "1",
            "side": "long" if leg["side"] == "buy" else "short",
        }
        for leg in entry_arguments()["legs"]
    ]


class FakeConnection:
    def __init__(
        self,
        *,
        mutation_error: bool = False,
        reconciled_status: str | None = None,
        activities: list[dict[str, Any]] | None = None,
        positions: list[dict[str, Any]] | None = None,
        mutation_status: str = "accepted",
        open_orders: list[dict[str, Any]] | None = None,
        account_overrides: dict[str, Any] | None = None,
    ):
        self.mutation_error = mutation_error
        self.reconciled_status = reconciled_status
        self.mutations = 0
        self.reads: list[str] = []
        self.activities = activities or []
        self.positions = positions or []
        self.mutation_status = mutation_status
        self.open_orders = open_orders or []
        self.account_overrides = account_overrides or {}

    async def call_system_read(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.reads.append(name)
        if name == "get_account_info":
            data: Any = {
                "id": "account-uuid",
                "status": "ACTIVE",
                "equity": "100000",
                "buying_power": "200000",
                "cash": "100000",
                "portfolio_value": "100000",
                "options_trading_level": 3,
                **self.account_overrides,
            }
        elif name == "get_account_config":
            data = {}
        elif name == "get_clock":
            data = {"is_open": True}
        elif name == "get_orders":
            data = {"result": self.open_orders}
        elif name == "get_all_positions":
            data = {"result": self.positions}
        elif name == "get_account_activities":
            data = {"result": self.activities}
        elif name == "get_order_by_client_id" and self.reconciled_status:
            data = {"id": "broker-order-1", "status": self.reconciled_status}
        else:
            raise RuntimeError("not found")
        return {"_alpaca_mcp_security": {"trust": "untrusted_tool_output"}, "data": data}

    async def call_mutation(self, name: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.mutations += 1
        if self.mutation_error:
            raise TimeoutError("ambiguous")
        return {
            "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
            "data": {"id": "broker-order-1", "status": self.mutation_status},
        }


def setup_runtime(tmp_path: Path, valid_env_file: Path, connection: FakeConnection):
    settings = load_settings(valid_env_file)
    object.__setattr__(settings, "read_only", False)
    object.__setattr__(settings, "execution_enabled", True)
    object.__setattr__(settings, "expected_account_id", "account-uuid")
    store = Store(tmp_path / "execution.sqlite3")
    store.initialize()
    store.bind_identity("development", "account-uuid", "account-uuid")
    store.arm_entry_authorization(
        "entry-auth-1",
        environment="development",
        account_id="account-uuid",
        strategy_date="2026-09-01",
        expires_at=NOW + timedelta(hours=1),
        requested_by="test_operator",
        reason="one paper-entry test",
        armed_at=NOW - timedelta(minutes=1),
    )
    store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="hash",
    )
    store.transition_strategy_run("run-1", "SCREENING", "WINDOW")
    store.transition_strategy_run("run-1", "AI_REVIEW", "ELIGIBLE")
    store.transition_strategy_run("run-1", "POLICY_CHECK", "MODEL_ALLOW")
    arguments = entry_arguments()
    store.record_order_intent(
        "intent-1",
        run_id="run-1",
        purpose="entry",
        client_order_id=arguments["client_order_id"],
        payload=arguments,
    )
    store.create_order_chain(
        "chain-1", run_id="run-1", intent_id="intent-1", purpose="entry"
    )
    service = ExecutionService(settings, store, connection)  # type: ignore[arg-type]
    return service, store, arguments


@pytest.mark.asyncio
async def test_exit_quote_rejects_unapproved_option_feed_before_mcp_read() -> None:
    connection = FakeConnection()

    with pytest.raises(ValueError, match="ALPACA_OPTION_FEED"):
        await quote_atomic_exit(
            connection,  # type: ignore[arg-type]
            entry_arguments(),
            now=NOW,
            option_feed="opra",
        )

    assert connection.reads == []


@pytest.mark.asyncio
async def test_entry_persists_before_single_mcp_mutation(tmp_path, valid_env_file) -> None:
    connection = FakeConnection()
    service, store, arguments = setup_runtime(tmp_path, valid_env_file, connection)
    result = await service.submit_entry(
        run_id="run-1",
        intent_id="intent-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        arguments=arguments,
        now=NOW,
    )
    assert result.state == "ORDER_PENDING"
    assert connection.mutations == 1
    assert store.get_strategy_run("run-1")["state"] == "ORDER_PENDING"
    assert [item["to_state"] for item in store.list_strategy_transitions("run-1")][-2:] == [
        "SUBMITTING",
        "ORDER_PENDING",
    ]


@pytest.mark.asyncio
async def test_ambiguous_timeout_reconciles_by_client_id_without_retry(
    tmp_path, valid_env_file
) -> None:
    connection = FakeConnection(mutation_error=True, reconciled_status="filled")
    service, store, arguments = setup_runtime(tmp_path, valid_env_file, connection)
    result = await service.submit_entry(
        run_id="run-1",
        intent_id="intent-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        arguments=arguments,
        now=NOW,
    )
    assert result.state == "POSITION_OPEN"
    assert result.reconciled_after_error is True
    assert connection.mutations == 1
    assert connection.reads[-1] == "get_order_by_client_id"
    assert store.get_strategy_run("run-1")["state"] == "POSITION_OPEN"


@pytest.mark.asyncio
async def test_unresolved_timeout_enters_risk_off_and_never_retries(
    tmp_path, valid_env_file
) -> None:
    connection = FakeConnection(mutation_error=True)
    service, store, arguments = setup_runtime(tmp_path, valid_env_file, connection)
    result = await service.submit_entry(
        run_id="run-1",
        intent_id="intent-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        arguments=arguments,
        now=NOW,
    )
    assert result.state == "RISK_OFF"
    assert result.detail["blind_retry"] is False
    assert connection.mutations == 1
    assert store.get_strategy_run("run-1")["state"] == "RISK_OFF"
    connection.reconciled_status = "accepted"
    reconciled = await service.reconcile_entry_order(
        run_id="run-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        client_order_id=arguments["client_order_id"],
    )
    assert reconciled is not None and reconciled.state == "RISK_OFF"
    assert store.get_order_chain("chain-1")["state"] == "PENDING"


@pytest.mark.asyncio
async def test_consumed_one_shot_authorization_blocks_a_second_entry_mutation(
    tmp_path, valid_env_file
) -> None:
    connection = FakeConnection()
    service, _, arguments = setup_runtime(tmp_path, valid_env_file, connection)

    first = await service.submit_entry(
        run_id="run-1",
        intent_id="intent-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        arguments=arguments,
        now=NOW,
    )
    assert first.state == "ORDER_PENDING"

    with pytest.raises(PolicyError, match="ENTRY_AUTHORIZATION_CONSUMED"):
        await service.submit_entry(
            run_id="run-1",
            intent_id="intent-1",
            chain_id="chain-1",
            attempt_id="attempt-1",
            arguments=arguments,
            now=NOW + timedelta(seconds=1),
        )
    assert connection.mutations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection", "reason"),
    [
        (
            FakeConnection(
                open_orders=[
                    {
                        "id": "stock-order",
                        "asset_class": "us_equity",
                        "status": "accepted",
                    }
                ]
            ),
            "OPEN_ORDER_EXISTS",
        ),
        (
            FakeConnection(account_overrides={"trading_blocked": True}),
            "ACCOUNT_TRADING_BLOCKED",
        ),
    ],
)
async def test_account_wide_admission_blocks_before_entry_mutation(
    tmp_path, valid_env_file, connection: FakeConnection, reason: str
) -> None:
    service, _, arguments = setup_runtime(tmp_path, valid_env_file, connection)

    with pytest.raises(PolicyError, match=reason):
        await service.submit_entry(
            run_id="run-1",
            intent_id="intent-1",
            chain_id="chain-1",
            attempt_id="attempt-1",
            arguments=arguments,
            now=NOW,
        )
    assert connection.mutations == 0


@pytest.mark.asyncio
async def test_tampered_durable_entry_is_rejected_before_broker_reads(
    tmp_path, valid_env_file
) -> None:
    connection = FakeConnection()
    service, _, arguments = setup_runtime(tmp_path, valid_env_file, connection)
    changed = deepcopy(arguments)
    changed["limit_price"] = "-0.80"
    with pytest.raises(Exception, match="durable order intent"):
        await service.submit_entry(
            run_id="run-1",
            intent_id="intent-1",
            chain_id="chain-1",
            attempt_id="attempt-1",
            arguments=changed,
            now=NOW,
        )
    assert connection.reads == []
    assert connection.mutations == 0


@pytest.mark.asyncio
async def test_fill_and_assignment_activities_are_idempotently_audited(
    tmp_path, valid_env_file
) -> None:
    activities = [
        {
            "id": "fill-activity-1",
            "activity_type": "FILL",
            "order_id": "broker-order-1",
            "symbol": "PANW260904P00100000",
            "side": "buy",
            "qty": "1",
            "price": "0.25",
            "transaction_time": NOW.isoformat(),
        },
        {
            "id": "assignment-1",
            "activity_type": "OPASN",
            "symbol": "PANW260904P00105000",
            "date": "2026-09-01",
        },
    ]
    connection = FakeConnection(activities=activities)
    service, store, arguments = setup_runtime(tmp_path, valid_env_file, connection)
    await service.submit_entry(
        run_id="run-1",
        intent_id="intent-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        arguments=arguments,
        now=NOW,
    )
    first = await service.reconcile_account_activities(
        run_id="run-1", after=NOW - timedelta(days=1), now=NOW
    )
    second = await service.reconcile_account_activities(
        run_id="run-1", after=NOW - timedelta(days=1), now=NOW
    )
    assert first.assignment_or_exercise_detected is True
    assert first.fills_recorded == second.fills_recorded == 1
    with store.connect() as database:
        assert database.execute("SELECT COUNT(*) FROM broker_activities").fetchone()[0] == 2
        assert database.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_filled_initial_and_reconciled_exit_wait_for_position_confirmation(
    tmp_path, valid_env_file
) -> None:
    connection = FakeConnection(
        positions=entry_positions(),
        mutation_status="filled",
        reconciled_status="filled",
    )
    service, store, _ = setup_runtime(tmp_path, valid_env_file, connection)
    store.transition_strategy_run("run-1", "SUBMITTING", "ENTRY_SUBMITTED")
    store.transition_strategy_run("run-1", "POSITION_OPEN", "ENTRY_FILLED")
    arguments = exit_arguments()
    store.record_order_intent(
        "exit-intent",
        run_id="run-1",
        purpose="exit",
        client_order_id=arguments["client_order_id"],
        payload=arguments,
    )
    store.create_order_chain(
        "exit-chain", run_id="run-1", intent_id="exit-intent", purpose="exit"
    )

    initial = await service.submit_exit(
        run_id="run-1",
        intent_id="exit-intent",
        chain_id="exit-chain",
        attempt_id="exit-attempt",
        arguments=arguments,
        now=NOW,
    )

    assert initial.state == "EXIT_PENDING"
    assert initial.broker_status == "filled"
    assert initial.detail["awaiting_position_reconciliation"] is True
    assert store.get_order_chain("exit-chain")["state"] == "FILLED"
    assert store.get_strategy_run("run-1")["state"] == "EXIT_PENDING"

    reconciled = await service.reconcile_exit_order(
        run_id="run-1",
        chain_id="exit-chain",
        attempt_id="exit-attempt",
        client_order_id=arguments["client_order_id"],
    )

    assert reconciled is not None
    assert reconciled.state == "EXIT_PENDING"
    assert reconciled.detail["awaiting_position_reconciliation"] is True
    assert store.get_order_chain("exit-chain")["state"] == "FILLED"
    assert store.get_strategy_run("run-1")["state"] == "EXIT_PENDING"
