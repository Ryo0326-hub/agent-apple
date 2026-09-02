from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from thetatrap import rehearsal
from thetatrap.execution import BrokerSnapshot
from thetatrap.runtime import ThetaTrapRuntime
from thetatrap.settings import StrategyProfile, load_settings
from thetatrap.storage import Store


NOW = datetime(2026, 8, 31, 18, 30, tzinfo=UTC)
STRATEGY_DATE = date(2026, 9, 1)


class Registry:
    required_schema_hash = "runtime-schema-hash"
    tool_count = 54


class ReadOnlyConnection:
    registry = Registry()

    def qwen_openai_tools(self, *, include_entry: bool = False) -> list[dict[str, Any]]:
        del include_entry
        return []

    async def call_system_read(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del arguments
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
        elif name in {"get_orders", "get_all_positions"}:
            data = {"result": []}
        else:
            raise AssertionError(f"unexpected read: {name}")
        return {"data": data}

    async def call_agent_read(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.call_system_read(name, arguments)


def _snapshot(*, market_is_open: bool = True) -> BrokerSnapshot:
    return BrokerSnapshot(
        observed_at=NOW,
        account={"id": "dev-account-id"},
        account_config={},
        clock={"is_open": market_is_open},
        open_orders=(),
        positions=(),
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        options_level=3,
        market_is_open=market_is_open,
    )


@pytest.mark.asyncio
async def test_runtime_rehearsal_selects_future_events_but_keeps_live_time(
    tmp_path: Path, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file).model_copy(
        update={"environment": "replay", "database_path": tmp_path / "rehearsal.db"}
    )
    store = Store(settings.database_path)
    store.initialize()
    runtime = ThetaTrapRuntime(settings, store, ReadOnlyConnection())  # type: ignore[arg-type]
    scan = AsyncMock(return_value={"status": "screening"})
    runtime._scan_entry = scan  # type: ignore[method-assign]

    result = await runtime.rehearse_entry(_snapshot(), NOW, strategy_date=STRATEGY_DATE)

    assert result == {"status": "screening"}
    _, called_now = scan.call_args.args
    assert called_now == NOW
    assert scan.call_args.kwargs["strategy_date_override"] == STRATEGY_DATE
    assert [event.symbol for event in scan.call_args.kwargs["events_override"]] == [
        "PANW",
        "MDB",
        "CRDO",
        "GTLB",
    ]


@pytest.mark.asyncio
async def test_runtime_rehearsal_rejects_non_replay_role(
    tmp_path: Path, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)
    store = Store(tmp_path / "runtime.db")
    store.initialize()
    runtime = ThetaTrapRuntime(settings, store, ReadOnlyConnection())  # type: ignore[arg-type]

    with pytest.raises(Exception, match="disarmed replay role"):
        await runtime.rehearse_entry(_snapshot(), NOW, strategy_date=STRATEGY_DATE)


@pytest.mark.asyncio
async def test_decision_rehearsal_uses_ephemeral_store_and_proves_no_mutation(
    monkeypatch: pytest.MonkeyPatch, valid_env_file: Path
) -> None:
    delegate = ReadOnlyConnection()

    @asynccontextmanager
    async def connection_factory(*_: Any) -> AsyncIterator[ReadOnlyConnection]:
        yield delegate

    async def fake_rehearse_entry(
        self: ThetaTrapRuntime,
        snapshot: BrokerSnapshot,
        now: datetime,
        *,
        strategy_date: date,
    ) -> dict[str, Any]:
        assert snapshot.market_is_open is True
        assert now == NOW
        events = tuple(
            event
            for event in self.events.events
            if event.status == "verified" and event.event_date == strategy_date
        )
        self._create_run(strategy_date, events)
        return {"status": "screening", "eligible_candidates": 0}

    monkeypatch.setattr(ThetaTrapRuntime, "rehearse_entry", fake_rehearse_entry)
    settings = load_settings(valid_env_file).model_copy(
        update={"featherless_api_key": SecretStr("test-featherless-key")}
    )
    source_database = Path(settings.database_path)
    source_existed_before = source_database.exists()

    result = await rehearsal.run_decision_rehearsal(
        settings,
        STRATEGY_DATE,
        now=NOW,
        connection_factory=connection_factory,  # type: ignore[arg-type]
    )

    assert result["outcome"] == "NO_ELIGIBLE_CANDIDATE"
    assert result["safety_status"] == "PASS"
    assert result["mode"] == "EPHEMERAL_LIVE_READ_REHEARSAL"
    assert result["production_database_touched"] is False
    assert result["strategy_profile"] == "earnings"
    assert result["strategy_symbols"] == ["PANW", "MDB", "CRDO", "GTLB"]
    assert result["event_symbols"] == ["PANW", "MDB", "CRDO", "GTLB"]
    assert result["broker_safety"] == {
        "mutation_dispatch_attempts": 0,
        "order_attempts_persisted": 0,
        "open_orders_before": 0,
        "open_orders_after": 0,
        "positions_before": 0,
        "positions_after": 0,
        "orders_unchanged": True,
        "positions_unchanged": True,
    }
    assert source_database.exists() is source_existed_before


@pytest.mark.asyncio
async def test_intraday_decision_rehearsal_uses_canary_runtime_and_version(
    monkeypatch: pytest.MonkeyPatch, valid_env_file: Path
) -> None:
    delegate = ReadOnlyConnection()

    @asynccontextmanager
    async def connection_factory(*_: Any) -> AsyncIterator[ReadOnlyConnection]:
        yield delegate

    async def fake_rehearse_intraday_entry(
        self: ThetaTrapRuntime,
        snapshot: BrokerSnapshot,
        now: datetime,
        *,
        strategy_date: date,
    ) -> dict[str, Any]:
        assert snapshot.market_is_open is True
        assert now == NOW
        assert strategy_date == date(2026, 9, 3)
        self.store.create_strategy_run(
            "tt-run-replay-2026-09-03-canary-test",
            environment="replay",
            strategy_date=strategy_date.isoformat(),
            strategy_version="2.0-sep3-canary",
            config_hash="canary-test-config-hash",
            context={"symbols": ["QQQ", "SPY"], "strategy_profile": "intraday_canary"},
            initial_state="SCREENING",
        )
        return {"status": "screening", "eligible_candidates": 0}

    monkeypatch.setattr(
        ThetaTrapRuntime,
        "rehearse_intraday_entry",
        fake_rehearse_intraday_entry,
        raising=False,
    )
    settings = load_settings(valid_env_file).model_copy(
        update={
            "featherless_api_key": SecretStr("test-featherless-key"),
            "strategy_profile": StrategyProfile.INTRADAY_CANARY,
        }
    )

    result = await rehearsal.run_decision_rehearsal(
        settings,
        date(2026, 9, 3),
        now=NOW,
        connection_factory=connection_factory,  # type: ignore[arg-type]
    )

    assert result["outcome"] == "NO_ELIGIBLE_CANDIDATE"
    assert result["safety_status"] == "PASS"
    assert result["strategy_profile"] == "intraday_canary"
    assert result["strategy_symbols"] == ["QQQ", "SPY"]
    assert result["event_symbols"] == []
    assert result["strategy_state"] == "SCREENING"
    assert result["broker_safety"]["mutation_dispatch_attempts"] == 0


@pytest.mark.asyncio
async def test_intraday_decision_rehearsal_rejects_non_sep3_date(
    valid_env_file: Path,
) -> None:
    settings = load_settings(valid_env_file).model_copy(
        update={
            "featherless_api_key": SecretStr("test-featherless-key"),
            "strategy_profile": StrategyProfile.INTRADAY_CANARY,
        }
    )

    with pytest.raises(Exception, match="limited to 2026-09-03"):
        await rehearsal.run_decision_rehearsal(
            settings,
            date(2026, 9, 2),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_mutation_proof_connection_rejects_dispatch() -> None:
    connection = rehearsal.MutationProofConnection(ReadOnlyConnection())  # type: ignore[arg-type]

    with pytest.raises(Exception, match="blocks every broker mutation"):
        await connection.call_mutation("place_option_order", {})

    assert connection.mutation_attempts == 1


def test_decision_status_never_labels_an_empty_trace_as_qwen_success() -> None:
    assert rehearsal._decision_status([], []) == "NO_ELIGIBLE_CANDIDATE"
    assert (
        rehearsal._decision_status(
            [{"status": "FAILED", "result": {"outcome": "ERROR"}}], []
        )
        == "QWEN_REVIEW_FAILED"
    )
    assert (
        rehearsal._decision_status(
            [{"status": "VETOED", "result": {"outcome": "VETO"}}],
            [{"tool_name": "record_candidate_rejection"}],
        )
        == "QWEN_DECISION_RECORDED"
    )
