from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from thetatrap import runtime as runtime_module
from thetatrap.agent import AgentContext, AgentDecision, AgentOutcome
from thetatrap.domain import (
    GateCode,
    GateFailure,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionSnapshot,
    StrategyEvaluation,
    UnderlyingQuote,
)
from thetatrap.errors import PolicyError
from thetatrap.execution import BrokerSnapshot, ExecutionResult
from thetatrap.intraday import (
    EXPIRATION,
    INTRADAY_PROFILE_ID,
    INTRADAY_STRATEGY_VERSION,
    evaluate_intraday_symbol,
)
from thetatrap.market import MarketCollection
from thetatrap.orders import (
    build_entry_order_intent,
    build_exit_from_entry_arguments,
    serialize_candidate,
)
from thetatrap.runtime import ThetaTrapRuntime
from thetatrap.schedule import ScheduleAction
from thetatrap.settings import StrategyProfile, load_settings
from thetatrap.storage import Store


D = Decimal
NOW = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)  # 10:00 ET
TRADE_DATE = date(2026, 9, 3)
OI_DATE = date(2026, 8, 31)


class Registry:
    required_schema_hash = "runtime-intraday-schema"


class StubConnection:
    registry = Registry()


def _store(tmp_path: Path, name: str = "runtime-intraday.sqlite3") -> Store:
    store = Store(tmp_path / name)
    store.initialize()
    return store


def _settings(valid_env_file: Path, **updates: Any) -> Any:
    return load_settings(valid_env_file).model_copy(
        update={
            "strategy_profile": StrategyProfile.INTRADAY_CANARY,
            **updates,
        }
    )


def _snapshot(
    *,
    observed_at: datetime = NOW,
    positions: tuple[dict[str, Any], ...] = (),
    open_orders: tuple[dict[str, Any], ...] = (),
) -> BrokerSnapshot:
    return BrokerSnapshot(
        observed_at=observed_at,
        account={"id": "dev-account-id", "status": "ACTIVE"},
        account_config={},
        clock={"is_open": True},
        open_orders=open_orders,
        positions=positions,
        equity=D("100000"),
        buying_power=D("200000"),
        options_level=3,
        market_is_open=True,
    )


def _option(
    symbol: str,
    strike: str,
    right: OptionRight,
    bid: str,
    ask: str,
    *,
    timestamp: datetime = NOW - timedelta(seconds=5),
) -> OptionSnapshot:
    strike_value = D(strike)
    right_letter = "C" if right is OptionRight.CALL else "P"
    return OptionSnapshot(
        contract=OptionContract(
            symbol=(
                f"{symbol}260904{right_letter}"
                f"{int(strike_value * D('1000')):08d}"
            ),
            underlying_symbol=symbol,
            expiration=EXPIRATION,
            right=right,
            strike=strike_value,
            tradable=True,
            status="active",
            multiplier=D("100"),
            size=D("100"),
            open_interest=1_000,
            open_interest_date=OI_DATE,
            ppind=True,
        ),
        quote=OptionQuote(
            bid=D(bid),
            ask=D(ask),
            timestamp=timestamp,
            implied_volatility=None,
            delta=None,
        ),
    )


def _chain(
    symbol: str = "QQQ", *, timestamp: datetime = NOW - timedelta(seconds=5)
) -> tuple[OptionSnapshot, ...]:
    return (
        _option(symbol, "498", OptionRight.PUT, "0.35", "0.40", timestamp=timestamp),
        _option(symbol, "499", OptionRight.PUT, "0.60", "0.65", timestamp=timestamp),
        _option(symbol, "501", OptionRight.CALL, "0.65", "0.70", timestamp=timestamp),
        _option(symbol, "502", OptionRight.CALL, "0.35", "0.40", timestamp=timestamp),
    )


def _collection(
    symbol: str = "QQQ",
    *,
    sequence: int = 0,
    quote_timestamp: datetime = NOW - timedelta(seconds=5),
    snapshots: tuple[OptionSnapshot, ...] | None = None,
) -> MarketCollection:
    chain = snapshots or _chain(symbol, timestamp=quote_timestamp)
    return MarketCollection(
        collection_id=f"{symbol.lower()}-collection-{sequence}",
        symbol=symbol,
        collected_at=NOW,
        previous_trading_day=date(2026, 9, 2),
        underlying=UnderlyingQuote(
            bid=D("499.99"),
            ask=D("500.01"),
            timestamp=NOW - timedelta(seconds=2),
        ),
        front_chain=chain,
        back_chain=chain,
        trade_expiration=EXPIRATION,
        term_expiration=EXPIRATION,
        source_digest=f"{symbol.lower()}-digest-{sequence}",
        diagnostics={"profile": "integration-test"},
    )


def _chain_with_tighter_alternative(
    *, fail_original_short_put: bool = False
) -> tuple[OptionSnapshot, ...]:
    original = list(_chain())
    if fail_original_short_put:
        original[1] = replace(
            original[1],
            quote=replace(
                original[1].quote,
                timestamp=NOW - timedelta(seconds=61),
            ),
        )
    return (
        *original,
        _option("QQQ", "496", OptionRight.PUT, "0.20", "0.21"),
        _option("QQQ", "497", OptionRight.PUT, "0.50", "0.51"),
        _option("QQQ", "503", OptionRight.CALL, "0.50", "0.51"),
        _option("QQQ", "504", OptionRight.CALL, "0.20", "0.21"),
    )


def _candidate(symbol: str = "QQQ") -> Any:
    collection = _collection(symbol)
    evaluation = evaluate_intraday_symbol(
        symbol=symbol,
        observed_at=collection.collected_at,
        underlying=collection.underlying,
        option_chain=collection.front_chain,
        buying_power=D("200000"),
    )
    assert evaluation.candidate is not None
    return evaluation.candidate


def _entry_positions(candidate: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "symbol": leg.snapshot.contract.symbol,
            "asset_class": "us_option",
            "qty": "1",
            "side": "long" if leg.side.value == "buy" else "short",
        }
        for leg in candidate.legs
    )


@pytest.mark.asyncio
async def test_profile_routing_creates_canary_run_without_earnings_events(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    collections = {symbol: _collection(symbol) for symbol in ("QQQ", "SPY")}
    collected_symbols: list[str] = []

    async def fake_collect(*_: Any, **kwargs: Any) -> MarketCollection:
        symbol = str(kwargs["symbol"])
        collected_symbols.append(symbol)
        return collections[symbol]

    def reject(**kwargs: Any) -> StrategyEvaluation:
        symbol = str(kwargs["symbol"])
        return StrategyEvaluation(
            symbol=symbol,
            candidate=None,
            failures=(
                GateFailure(GateCode.NO_VALID_CONDOR, "integration rejection"),
            ),
        )

    monkeypatch.setattr(runtime_module, "collect_symbol_market", fake_collect)
    monkeypatch.setattr(runtime_module, "evaluate_intraday_symbol", reject)
    advisory = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(
        runtime_module, "run_rejected_candidate_advisory", advisory
    )

    assert not any(
        event.status == "verified" and event.event_date == TRADE_DATE
        for event in runtime.events.events
    )
    result = await runtime._scan_profile_entry(_snapshot(), NOW)

    assert result["status"] == "screening"
    assert result["strategy_profile"] == "intraday_canary"
    assert collected_symbols == ["QQQ", "SPY"]
    run = store.find_strategy_run(
        environment="development",
        strategy_date="2026-09-03",
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    assert run is not None
    assert run["state"] == "SCREENING"
    assert run["context"]["strategy_profile_id"] == INTRADAY_PROFILE_ID
    assert run["context"]["symbols"] == ["QQQ", "SPY"]
    advisory.assert_awaited_once()


@pytest.mark.asyncio
async def test_eligible_canary_reaches_qwen_review_and_fresh_policy_path(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    reviewed: list[AgentContext] = []

    class AllowAgent:
        async def review(self, context: AgentContext) -> AgentDecision:
            reviewed.append(context)
            return AgentDecision(
                outcome=AgentOutcome.ALLOW,
                model="mock-qwen",
                explanation="bounded candidate accepted by integration mock",
                mutation_arguments=context.order_arguments,
                turns=1,
                tool_calls=0,
            )

    runtime = ThetaTrapRuntime(
        settings,
        store,
        StubConnection(),  # type: ignore[arg-type]
        agent_factory=lambda *_: AllowAgent(),  # type: ignore[arg-type]
    )
    call_counts = {"QQQ": 0, "SPY": 0}

    async def fake_collect(*_: Any, **kwargs: Any) -> MarketCollection:
        symbol = str(kwargs["symbol"])
        sequence = call_counts[symbol]
        call_counts[symbol] += 1
        return _collection(symbol, sequence=sequence)

    monkeypatch.setattr(runtime_module, "collect_symbol_market", fake_collect)

    result = await runtime._scan_profile_entry(_snapshot(), NOW)

    assert result == {
        "status": "shadow_allow",
        "executed": False,
        "candidate": "QQQ",
    }
    assert call_counts == {"QQQ": 2, "SPY": 1}
    assert len(reviewed) == 1
    context = reviewed[0]
    assert context.symbol == "QQQ"
    assert context.event["event_dependency"] is False
    assert context.event["strategy_profile"] == "intraday_canary"
    assert context.run_summary["strategy_profile"] == "intraday_canary"
    assert context.order_arguments["limit_price"] == "-0.55"
    run = store.find_strategy_run(
        environment="development",
        strategy_date="2026-09-03",
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    assert run is not None
    assert run["state"] == "NO_TRADE"
    assert len(store.list_order_intents(run["run_id"], purpose="entry")) == 1
    with store.connect() as connection:
        agent = connection.execute(
            "SELECT status, model FROM agent_runs WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
    assert agent is not None
    assert dict(agent) == {
        "status": "COMPLETED",
        "model": (
            settings.featherless_primary_model
            + "|fallback="
            + settings.featherless_fallback_model
        ),
    }


@pytest.mark.asyncio
async def test_fresh_revalidation_accepts_same_legs_and_rejects_stale_refresh(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    original = _candidate()
    intent = build_entry_order_intent(
        original,
        environment=settings.environment,
        account_id="dev-account-id",
        event_date=TRADE_DATE,
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    refreshes = [
        _collection("QQQ", sequence=1),
        _collection(
            "QQQ",
            sequence=2,
            quote_timestamp=NOW - timedelta(seconds=61),
        ),
    ]
    calls: list[dict[str, Any]] = []

    async def fake_collect(*_: Any, **kwargs: Any) -> MarketCollection:
        calls.append(dict(kwargs))
        return refreshes.pop(0)

    monkeypatch.setattr(runtime_module, "collect_symbol_market", fake_collect)

    accepted = await runtime._fresh_revalidate_intraday(
        run, _snapshot(), original, intent
    )
    rejected = await runtime._fresh_revalidate_intraday(
        run, _snapshot(), original, intent
    )

    assert accepted is True
    assert rejected is False
    assert len(calls) == 2
    assert all(call["symbol"] == "QQQ" for call in calls)
    assert all(call["trade_expiration"] == EXPIRATION for call in calls)
    assert all(call["term_expiration"] == EXPIRATION for call in calls)
    assert all(call["stock_feed"] == "iex" for call in calls)
    assert all(call["option_feed"] == "indicative" for call in calls)
    rejected_rows = [
        candidate
        for candidate in store.list_candidates(run["run_id"])
        if candidate["eligible"] is False
    ]
    assert len(rejected_rows) == 1
    assert GateCode.OPTION_QUOTE_STALE.value in {
        gate["gate_name"]
        for gate in store.list_gate_results(rejected_rows[0]["candidate_id"])
    }


@pytest.mark.asyncio
async def test_fresh_rank_change_does_not_reject_still_valid_original_structure(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    original = _candidate()
    intent = build_entry_order_intent(
        original,
        environment=settings.environment,
        account_id="dev-account-id",
        event_date=TRADE_DATE,
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    fresh = _collection(
        "QQQ",
        sequence=10,
        snapshots=_chain_with_tighter_alternative(),
    )
    newly_ranked = evaluate_intraday_symbol(
        symbol="QQQ",
        observed_at=fresh.collected_at,
        underlying=fresh.underlying,
        option_chain=fresh.front_chain,
        buying_power=D("200000"),
    ).candidate
    assert newly_ranked is not None
    original_symbols = [leg.snapshot.contract.symbol for leg in original.legs]
    assert [leg.snapshot.contract.symbol for leg in newly_ranked.legs] != original_symbols
    monkeypatch.setattr(
        runtime_module,
        "collect_symbol_market",
        AsyncMock(return_value=fresh),
    )

    accepted = await runtime._fresh_revalidate_intraday(
        run, _snapshot(), original, intent
    )

    assert accepted is True


@pytest.mark.asyncio
async def test_failed_original_leg_rejects_even_when_new_top_rank_is_valid(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    original = _candidate()
    intent = build_entry_order_intent(
        original,
        environment=settings.environment,
        account_id="dev-account-id",
        event_date=TRADE_DATE,
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    fresh = _collection(
        "QQQ",
        sequence=11,
        snapshots=_chain_with_tighter_alternative(fail_original_short_put=True),
    )
    newly_ranked = evaluate_intraday_symbol(
        symbol="QQQ",
        observed_at=fresh.collected_at,
        underlying=fresh.underlying,
        option_chain=fresh.front_chain,
        buying_power=D("200000"),
    ).candidate
    assert newly_ranked is not None
    assert newly_ranked.short_put.snapshot.contract.strike == D("497")
    monkeypatch.setattr(
        runtime_module,
        "collect_symbol_market",
        AsyncMock(return_value=fresh),
    )

    accepted = await runtime._fresh_revalidate_intraday(
        run, _snapshot(), original, intent
    )

    assert accepted is False
    rejected = [
        candidate
        for candidate in store.list_candidates(run["run_id"])
        if candidate["eligible"] is False
    ]
    assert len(rejected) == 1
    assert GateCode.OPTION_QUOTE_STALE.value in {
        gate["gate_name"]
        for gate in store.list_gate_results(rejected[0]["candidate_id"])
    }


@pytest.mark.asyncio
async def test_canary_entry_reprice_uses_twenty_cent_floor_not_natural_credit(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    candidate = _candidate()
    assert candidate.natural_credit == D("0.45")
    candidate_id = "canary-reprice-candidate"
    store.record_candidate(
        candidate_id,
        run_id=run["run_id"],
        symbol="QQQ",
        eligible=True,
        candidate_rank=1,
        payload=serialize_candidate(candidate),
    )
    built = build_entry_order_intent(
        candidate,
        environment=settings.environment,
        account_id="dev-account-id",
        event_date=TRADE_DATE,
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    payload = {**built.arguments, "limit_price": "-0.25"}
    store.record_order_intent(
        built.intent_id,
        run_id=run["run_id"],
        candidate_id=candidate_id,
        purpose="entry",
        client_order_id=built.client_order_id,
        payload=payload,
    )
    store.create_order_chain(
        built.chain_id,
        run_id=run["run_id"],
        intent_id=built.intent_id,
        purpose="entry",
    )
    store.transition_order_chain(built.chain_id, "SUBMITTING")
    store.record_order_attempt(
        built.attempt_id,
        chain_id=built.chain_id,
        sequence=0,
        client_order_id=built.client_order_id,
        request=payload,
        broker_order_id="canary-entry-broker-id",
    )
    store.transition_order_chain(
        built.chain_id, "PENDING", attempt_id=built.attempt_id
    )
    now = datetime(2026, 9, 3, 14, 1, tzinfo=UTC)
    with store.connect() as connection:
        connection.execute(
            "UPDATE order_attempts SET created_at=? WHERE attempt_id=?",
            ((now - timedelta(seconds=31)).isoformat(), built.attempt_id),
        )
    replacement = AsyncMock(
        return_value=ExecutionResult(
            state="PENDING",
            broker_status="new",
            broker_order_id="replacement-broker-id",
            reconciled_after_error=False,
            detail={},
        )
    )
    monkeypatch.setattr(runtime.execution, "replace_order", replacement)

    result = await runtime._reprice_active_order(
        run, purpose="entry", now=now
    )

    assert result["status"] == "pending"
    assert replacement.await_args.kwargs["limit_price"] == "-0.2"
    assert D(replacement.await_args.kwargs["limit_price"]) == D("-0.20")


@pytest.mark.asyncio
async def test_1525_exit_starts_at_full_wing_without_requesting_fresh_quote(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    candidate = _candidate()
    entry = build_entry_order_intent(
        candidate,
        environment=settings.environment,
        account_id="dev-account-id",
        event_date=TRADE_DATE,
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    runtime._persist_intent(run["run_id"], None, entry)
    quote = AsyncMock(side_effect=AssertionError("aggressive exit must not quote"))
    monkeypatch.setattr(runtime_module, "quote_atomic_exit", quote)
    submit = AsyncMock(
        return_value=ExecutionResult(
            state="PENDING",
            broker_status="new",
            broker_order_id="canary-exit-broker-id",
            reconciled_after_error=False,
            detail={},
        )
    )
    monkeypatch.setattr(runtime.execution, "submit_exit", submit)
    now = datetime(2026, 9, 3, 19, 25, tzinfo=UTC)  # 15:25 ET
    snapshot = _snapshot(
        observed_at=now,
        positions=_entry_positions(candidate),
    )

    result = await runtime._start_exit(run, snapshot, now)

    assert result["status"] == "pending"
    quote.assert_not_awaited()
    arguments = submit.await_args.kwargs["arguments"]
    assert D(arguments["limit_price"]) == D("1.00")
    assert all(
        leg["position_intent"].endswith("_to_close") for leg in arguments["legs"]
    )


@pytest.mark.asyncio
async def test_outer_intact_position_then_inner_empty_snapshot_fails_risk_off(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        valid_env_file,
        read_only=False,
        execution_enabled=True,
    )
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "POSITION_OPEN"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    candidate = _candidate()
    entry = build_entry_order_intent(
        candidate,
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="outer-position-inner-flat",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    now = datetime(2026, 9, 3, 19, 25, tzinfo=UTC)  # 15:25 ET
    outer_snapshot = _snapshot(
        observed_at=now,
        positions=_entry_positions(candidate),
    )
    monkeypatch.setattr(
        runtime.execution,
        "read_broker_snapshot",
        AsyncMock(return_value=_snapshot(observed_at=now)),
    )

    result = await runtime._start_exit(run, outer_snapshot, now)

    assert result["status"] == "risk_off"
    assert store.get_strategy_run(run["run_id"])["state"] == "RISK_OFF"
    assert store.get_kill_switch()["kill_switch_enabled"] is True
    exit_chains = store.list_order_chains(run["run_id"], purpose="exit")
    assert len(exit_chains) == 1 and exit_chains[0]["state"] == "PLANNED"


@pytest.mark.asyncio
async def test_stale_previous_screening_run_cannot_block_sep3_canary_scan(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    old = store.create_strategy_run(
        "sep2-stale-screening",
        environment="development",
        strategy_date="2026-09-02",
        strategy_version="1.2",
        config_hash="stale-config",
        context={"strategy_profile": "earnings"},
        initial_state="SCREENING",
    )
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    now = datetime(2026, 9, 3, 13, 45, tzinfo=UTC)  # 09:45 ET
    snapshot = _snapshot(observed_at=now)
    monkeypatch.setattr(
        runtime.execution,
        "read_broker_snapshot",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(
        runtime,
        "_reconcile_active",
        AsyncMock(return_value=old),
    )

    async def create_canary(*_: Any, **__: Any) -> dict[str, Any]:
        runtime._create_intraday_run()
        return {"status": "canary_scan_started"}

    scan = AsyncMock(side_effect=create_canary)
    monkeypatch.setattr(runtime, "_scan_profile_entry", scan)

    result = await runtime._cycle_locked(now)

    assert result["status"] == "canary_scan_started"
    scan.assert_awaited_once_with(snapshot, now)
    stale = store.get_strategy_run(old["run_id"])
    assert stale is not None
    assert stale["state"] == "NO_TRADE"
    assert store.list_strategy_transitions(old["run_id"])[-1]["reason_code"] == (
        "STALE_ENTRY_SESSION_CLOSED"
    )
    canary = store.find_strategy_run(
        environment="development",
        strategy_date="2026-09-03",
        strategy_version=INTRADAY_STRATEGY_VERSION,
    )
    assert canary is not None
    assert canary["context"]["strategy_profile"] == "intraday_canary"


@pytest.mark.asyncio
async def test_intraday_rehearsal_requires_replay_and_disarmed_role(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    development = _settings(valid_env_file)
    development_runtime = ThetaTrapRuntime(
        development, _store(tmp_path, "development.sqlite3"), StubConnection()  # type: ignore[arg-type]
    )
    with pytest.raises(PolicyError, match="disarmed replay role"):
        await development_runtime.rehearse_intraday_entry(
            _snapshot(), NOW, strategy_date=TRADE_DATE
        )

    armed_replay = _settings(
        valid_env_file,
        environment="replay",
        database_path=tmp_path / "armed-replay.sqlite3",
        read_only=False,
        execution_enabled=True,
    )
    armed_runtime = ThetaTrapRuntime(
        armed_replay, _store(tmp_path, "armed-replay.sqlite3"), StubConnection()  # type: ignore[arg-type]
    )
    with pytest.raises(PolicyError, match="disarmed replay role"):
        await armed_runtime.rehearse_intraday_entry(
            _snapshot(), NOW, strategy_date=TRADE_DATE
        )

    disarmed_replay = _settings(
        valid_env_file,
        environment="replay",
        database_path=tmp_path / "disarmed-replay.sqlite3",
        read_only=True,
        execution_enabled=False,
    )
    replay_runtime = ThetaTrapRuntime(
        disarmed_replay,
        _store(tmp_path, "disarmed-replay.sqlite3"),
        StubConnection(),  # type: ignore[arg-type]
    )
    scan = AsyncMock(return_value={"status": "screening"})
    monkeypatch.setattr(replay_runtime, "_scan_intraday_entry", scan)

    result = await replay_runtime.rehearse_intraday_entry(
        _snapshot(), NOW, strategy_date=TRADE_DATE
    )

    assert result == {"status": "screening"}
    scan.assert_awaited_once_with(
        _snapshot(),
        NOW,
        strategy_date_override=TRADE_DATE,
    )


@pytest.mark.asyncio
async def test_repeated_veto_cycles_keep_unique_audit_records(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)

    class VetoAgent:
        async def review(self, _: AgentContext) -> AgentDecision:
            return AgentDecision(
                outcome=AgentOutcome.VETO,
                model="mock-qwen",
                explanation="bounded mock veto",
                reason_code="TRADING_HALT",
                turns=1,
                tool_calls=0,
            )

    runtime = ThetaTrapRuntime(
        settings,
        store,
        StubConnection(),  # type: ignore[arg-type]
        agent_factory=lambda *_: VetoAgent(),  # type: ignore[arg-type]
    )
    counts = {"QQQ": 0, "SPY": 0}

    async def fake_collect(*_: Any, **kwargs: Any) -> MarketCollection:
        symbol = str(kwargs["symbol"])
        counts[symbol] += 1
        return _collection(symbol, sequence=counts[symbol])

    monkeypatch.setattr(runtime_module, "collect_symbol_market", fake_collect)

    first = await runtime._scan_profile_entry(_snapshot(), NOW)
    run = store.find_active_strategy_run(environment=settings.environment)
    assert run is not None and run["state"] == "SCREENING"
    cooldown = await runtime._scan_profile_entry(
        _snapshot(), NOW + timedelta(minutes=1), existing_run=run
    )
    second = await runtime._scan_profile_entry(
        _snapshot(), NOW + timedelta(minutes=6), existing_run=run
    )

    assert first["status"] == second["status"] == "screening"
    assert cooldown["reason"] == "QWEN_REVIEW_COOLDOWN"
    candidates = store.list_candidates(run["run_id"])
    assert [candidate["candidate_rank"] for candidate in candidates] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    intents = store.list_order_intents(run["run_id"], purpose="entry")
    assert len(intents) == 4
    assert len({intent["intent_id"] for intent in intents}) == 4


@pytest.mark.asyncio
async def test_consumed_second_candidate_drives_position_check_and_exit(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        valid_env_file,
        read_only=False,
        execution_enabled=True,
    )
    store = _store(tmp_path)
    store.bind_identity(
        settings.environment,
        settings.expected_account_id,
        settings.expected_account_id,
    )
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")

    qqq = _candidate("QQQ")
    spy = _candidate("SPY")
    qqq_intent = build_entry_order_intent(
        qqq,
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="canary-qqq-observation",
    )
    spy_intent = build_entry_order_intent(
        spy,
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="canary-spy-observation",
    )
    runtime._persist_intent(run["run_id"], None, qqq_intent)
    runtime._persist_intent(run["run_id"], None, spy_intent)
    authorization = store.arm_entry_authorization(
        "canary-second-candidate-auth",
        environment=settings.environment,
        account_id=settings.expected_account_id,
        strategy_date=TRADE_DATE.isoformat(),
        expires_at=NOW + timedelta(hours=1),
        requested_by="test_operator",
        reason="verify selected-candidate linkage",
        armed_at=NOW - timedelta(minutes=1),
    )
    store.begin_authorized_entry_submission(
        authorization["authorization_id"],
        environment=settings.environment,
        account_id=settings.expected_account_id,
        strategy_date=TRADE_DATE.isoformat(),
        run_id=run["run_id"],
        intent_id=spy_intent.intent_id,
        chain_id=spy_intent.chain_id,
        attempt_id=spy_intent.attempt_id,
        client_order_id=spy_intent.client_order_id,
        request=spy_intent.arguments,
        observed_at=NOW,
    )
    store.transition_order_chain(
        spy_intent.chain_id, "FILLED", attempt_id=spy_intent.attempt_id
    )
    run = store.transition_strategy_run(
        run["run_id"], "POSITION_OPEN", "TEST_SECOND_CANDIDATE_FILLED"
    )
    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 3, 19, 25, tzinfo=UTC),
        positions=_entry_positions(spy),
    )
    submit = AsyncMock(
        return_value=ExecutionResult(
            state="PENDING",
            broker_status="new",
            broker_order_id="spy-exit-order",
            reconciled_after_error=False,
            detail={},
        )
    )
    monkeypatch.setattr(runtime.execution, "submit_exit", submit)

    intact, _ = runtime._intact_entry_position(run, snapshot)
    result = await runtime._start_exit(run, snapshot, snapshot.observed_at)

    assert intact is True
    assert result["status"] == "pending"
    assert {
        leg["symbol"] for leg in submit.await_args.kwargs["arguments"]["legs"]
    } == {leg.snapshot.contract.symbol for leg in spy.legs}


@pytest.mark.asyncio
async def test_planned_exit_is_submitted_after_restart(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        valid_env_file,
        read_only=False,
        execution_enabled=True,
    )
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "POSITION_OPEN"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    candidate = _candidate()
    entry = build_entry_order_intent(
        candidate,
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="restart-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    exit_intent = build_exit_from_entry_arguments(
        entry.arguments,
        limit_debit=D("1.00"),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="restart-exit",
    )
    runtime._persist_intent(run["run_id"], None, exit_intent)
    submit = AsyncMock(
        return_value=ExecutionResult(
            state="EXIT_PENDING",
            broker_status="accepted",
            broker_order_id="restart-exit-order",
            reconciled_after_error=False,
            detail={},
        )
    )
    monkeypatch.setattr(runtime.execution, "submit_exit", submit)

    result = await runtime._reprice_active_order(run, purpose="exit", now=NOW)

    assert result["action"] == "exit_resume_planned"
    assert submit.await_args.kwargs["intent_id"] == exit_intent.intent_id
    assert submit.await_args.kwargs["arguments"] == exit_intent.arguments
    assert submit.await_args.kwargs["attempt_id"].startswith("tt-attempt-resume-")


@pytest.mark.asyncio
async def test_submitting_exit_routes_to_exact_resume_after_restart(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in (
        "SCREENING",
        "AI_REVIEW",
        "POLICY_CHECK",
        "SUBMITTING",
        "POSITION_OPEN",
        "EXIT_SUBMITTING",
    ):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    candidate = _candidate()
    entry = build_entry_order_intent(
        candidate,
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="submitting-restart-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    exit_intent = build_exit_from_entry_arguments(
        entry.arguments,
        limit_debit=D("1.00"),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="submitting-restart-exit",
    )
    runtime._persist_intent(run["run_id"], None, exit_intent)
    store.transition_order_chain(exit_intent.chain_id, "SUBMITTING")
    store.record_order_attempt(
        exit_intent.attempt_id,
        chain_id=exit_intent.chain_id,
        sequence=0,
        client_order_id=exit_intent.client_order_id,
        request=exit_intent.arguments,
    )
    resume = AsyncMock(
        return_value=ExecutionResult(
            state="EXIT_PENDING",
            broker_status="accepted",
            broker_order_id="resumed-exit-order",
            reconciled_after_error=False,
            detail={},
        )
    )
    monkeypatch.setattr(runtime.execution, "resume_exit_submission", resume)

    result = await runtime._reprice_active_order(run, purpose="exit", now=NOW)

    assert result["action"] == "exit_resume_submitting"
    resume.assert_awaited_once()
    assert resume.await_args.kwargs["arguments"] == exit_intent.arguments


@pytest.mark.asyncio
async def test_submitting_exit_without_attempt_resumes_pre_dispatch_boundary(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in (
        "SCREENING",
        "AI_REVIEW",
        "POLICY_CHECK",
        "SUBMITTING",
        "POSITION_OPEN",
        "EXIT_SUBMITTING",
    ):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    entry = build_entry_order_intent(
        _candidate(),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="pre-dispatch-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    exit_intent = build_exit_from_entry_arguments(
        entry.arguments,
        limit_debit=D("1.00"),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="pre-dispatch-exit",
    )
    runtime._persist_intent(run["run_id"], None, exit_intent)
    store.transition_order_chain(exit_intent.chain_id, "SUBMITTING")
    submit = AsyncMock(
        return_value=ExecutionResult(
            state="EXIT_PENDING",
            broker_status="accepted",
            broker_order_id="resumed-pre-dispatch-exit",
            reconciled_after_error=False,
            detail={},
        )
    )
    monkeypatch.setattr(runtime.execution, "submit_exit", submit)

    result = await runtime._reprice_active_order(run, purpose="exit", now=NOW)

    assert result["action"] == "exit_resume_pre_dispatch"
    submit.assert_awaited_once()
    assert submit.await_args.kwargs["arguments"] == exit_intent.arguments
    assert submit.await_args.kwargs["attempt_id"].startswith("tt-attempt-resume-")


@pytest.mark.asyncio
async def test_replacement_pending_without_broker_id_routes_to_resume(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "ORDER_PENDING"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    candidate = _candidate()
    entry = build_entry_order_intent(
        candidate,
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="replacement-restart-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    store.transition_order_chain(entry.chain_id, "SUBMITTING")
    store.record_order_attempt(
        entry.attempt_id,
        chain_id=entry.chain_id,
        sequence=0,
        client_order_id=entry.client_order_id,
        request=entry.arguments,
        broker_order_id="original-entry-order",
    )
    store.transition_order_chain(
        entry.chain_id, "PENDING", attempt_id=entry.attempt_id
    )
    store.transition_order_chain(entry.chain_id, "REPLACEMENT_PENDING")
    replacement_id = "tt-replacement-restart"
    store.record_order_attempt(
        "replacement-restart-attempt",
        chain_id=entry.chain_id,
        sequence=1,
        client_order_id=replacement_id,
        request={
            "order_id": "original-entry-order",
            "limit_price": "-0.50",
            "client_order_id": replacement_id,
        },
    )
    resume = AsyncMock(
        return_value=ExecutionResult(
            state="PENDING",
            broker_status="accepted",
            broker_order_id="replacement-entry-order",
            reconciled_after_error=True,
            detail={},
        )
    )
    monkeypatch.setattr(runtime.execution, "resume_replacement", resume)

    result = await runtime._reprice_active_order(run, purpose="entry", now=NOW)

    assert result["status"] == "pending"
    resume.assert_awaited_once_with(
        chain_id=entry.chain_id,
        intent_id=entry.intent_id,
        attempt_id="replacement-restart-attempt",
    )


@pytest.mark.asyncio
async def test_entry_fill_lookup_cannot_use_stale_flat_snapshot_to_close_run(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    entry = build_entry_order_intent(
        _candidate(),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="stale-flat-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    store.transition_order_chain(entry.chain_id, "SUBMITTING")
    store.record_order_attempt(
        entry.attempt_id,
        chain_id=entry.chain_id,
        sequence=0,
        client_order_id=entry.client_order_id,
        request=entry.arguments,
    )
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_account_activities",
        AsyncMock(
            return_value=SimpleNamespace(
                assignment_or_exercise_detected=False,
                activity_types=(),
            )
        ),
    )

    async def fill_entry(**_: Any) -> ExecutionResult:
        store.transition_order_chain(
            entry.chain_id, "FILLED", attempt_id=entry.attempt_id
        )
        store.transition_strategy_run(
            run["run_id"], "POSITION_OPEN", "TEST_BROKER_FILL"
        )
        return ExecutionResult(
            state="POSITION_OPEN",
            broker_status="filled",
            broker_order_id="filled-entry-order",
            reconciled_after_error=True,
            detail={},
        )

    monkeypatch.setattr(runtime.execution, "reconcile_entry_order", fill_entry)

    reconciled = await runtime._reconcile_active(run, _snapshot())

    assert reconciled is not None and reconciled["state"] == "POSITION_OPEN"
    assert store.get_strategy_run(run["run_id"])["state"] == "POSITION_OPEN"


@pytest.mark.asyncio
async def test_next_cycle_stale_flat_snapshot_cannot_close_filled_entry_without_exit(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "POSITION_OPEN"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    entry = build_entry_order_intent(
        _candidate(),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="next-cycle-stale-position-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    store.transition_order_chain(entry.chain_id, "SUBMITTING")
    store.record_order_attempt(
        entry.attempt_id,
        chain_id=entry.chain_id,
        sequence=0,
        client_order_id=entry.client_order_id,
        request=entry.arguments,
        broker_order_id="filled-entry-awaiting-position-view",
    )
    store.transition_order_chain(entry.chain_id, "FILLED", attempt_id=entry.attempt_id)
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_account_activities",
        AsyncMock(
            return_value=SimpleNamespace(
                assignment_or_exercise_detected=False,
                activity_types=(),
            )
        ),
    )

    reconciled = await runtime._reconcile_active(run, _snapshot())

    assert reconciled is not None and reconciled["state"] == "POSITION_OPEN"
    assert store.get_strategy_run(run["run_id"])["state"] == "POSITION_OPEN"
    assert store.list_order_chains(run["run_id"], purpose="exit") == []


@pytest.mark.asyncio
async def test_rejected_exit_and_stale_flat_snapshot_cannot_close_filled_entry(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in (
        "SCREENING",
        "AI_REVIEW",
        "POLICY_CHECK",
        "SUBMITTING",
        "POSITION_OPEN",
        "EXIT_SUBMITTING",
        "RISK_OFF",
    ):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    entry = build_entry_order_intent(
        _candidate(),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="rejected-exit-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    store.transition_order_chain(entry.chain_id, "SUBMITTING")
    store.record_order_attempt(
        entry.attempt_id,
        chain_id=entry.chain_id,
        sequence=0,
        client_order_id=entry.client_order_id,
        request=entry.arguments,
        broker_order_id="filled-entry-before-rejected-exit",
    )
    store.transition_order_chain(entry.chain_id, "FILLED", attempt_id=entry.attempt_id)
    exit_intent = build_exit_from_entry_arguments(
        entry.arguments,
        limit_debit=D("1.00"),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="rejected-exit",
    )
    runtime._persist_intent(run["run_id"], None, exit_intent)
    store.transition_order_chain(exit_intent.chain_id, "SUBMITTING")
    store.record_order_attempt(
        exit_intent.attempt_id,
        chain_id=exit_intent.chain_id,
        sequence=0,
        client_order_id=exit_intent.client_order_id,
        request=exit_intent.arguments,
        broker_order_id="rejected-exit-order",
    )
    store.transition_order_chain(
        exit_intent.chain_id,
        "REJECTED",
        attempt_id=exit_intent.attempt_id,
    )
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_account_activities",
        AsyncMock(
            return_value=SimpleNamespace(
                assignment_or_exercise_detected=False,
                activity_types=(),
            )
        ),
    )
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_exit_order",
        AsyncMock(return_value=None),
    )

    reconciled = await runtime._reconcile_active(run, _snapshot())

    assert reconciled is not None and reconciled["state"] == "RISK_OFF"
    assert store.get_strategy_run(run["run_id"])["state"] == "RISK_OFF"


@pytest.mark.asyncio
async def test_unknown_chain_blocks_flat_reconciliation(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "RISK_OFF"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    entry = build_entry_order_intent(
        _candidate(),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="unknown-entry",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    store.transition_order_chain(entry.chain_id, "SUBMITTING")
    store.record_order_attempt(
        entry.attempt_id,
        chain_id=entry.chain_id,
        sequence=0,
        client_order_id=entry.client_order_id,
        request=entry.arguments,
    )
    store.transition_order_chain(
        entry.chain_id, "UNKNOWN", attempt_id=entry.attempt_id
    )
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_account_activities",
        AsyncMock(
            return_value=SimpleNamespace(
                assignment_or_exercise_detected=False,
                activity_types=(),
            )
        ),
    )
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_entry_order",
        AsyncMock(return_value=None),
    )

    reconciled = await runtime._reconcile_active(run, _snapshot())

    assert reconciled is not None and reconciled["state"] == "RISK_OFF"
    assert store.get_strategy_run(run["run_id"])["state"] == "RISK_OFF"


@pytest.mark.asyncio
async def test_terminal_zero_fill_entry_chain_recovers_run_to_no_trade(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK", "SUBMITTING", "ORDER_PENDING"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    entry = build_entry_order_intent(
        _candidate(),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="terminal-before-run-transition",
    )
    runtime._persist_intent(run["run_id"], None, entry)
    store.transition_order_chain(entry.chain_id, "SUBMITTING")
    store.record_order_attempt(
        entry.attempt_id,
        chain_id=entry.chain_id,
        sequence=0,
        client_order_id=entry.client_order_id,
        request=entry.arguments,
        broker_order_id="terminal-zero-fill-entry",
    )
    store.transition_order_chain(entry.chain_id, "PENDING", attempt_id=entry.attempt_id)
    store.transition_order_chain(entry.chain_id, "CANCELED", attempt_id=entry.attempt_id)
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_account_activities",
        AsyncMock(
            return_value=SimpleNamespace(
                assignment_or_exercise_detected=False,
                activity_types=(),
            )
        ),
    )
    reconcile_entry = AsyncMock(
        side_effect=AssertionError("terminal chain must not be queried as working")
    )
    monkeypatch.setattr(runtime.execution, "reconcile_entry_order", reconcile_entry)

    reconciled = await runtime._reconcile_active(run, _snapshot())

    assert reconciled is not None and reconciled["state"] == "NO_TRADE"
    assert store.get_strategy_run(run["run_id"])["state"] == "NO_TRADE"
    reconcile_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_vetoed_planned_chain_does_not_block_flat_after_round_trip(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    store.bind_identity(
        settings.environment,
        settings.expected_account_id,
        settings.expected_account_id,
    )
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in ("SCREENING", "AI_REVIEW", "POLICY_CHECK"):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")

    vetoed = build_entry_order_intent(
        _candidate("QQQ"),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="vetoed-planned-observation",
    )
    active = build_entry_order_intent(
        _candidate("SPY"),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="active-filled-observation",
    )
    runtime._persist_intent(run["run_id"], None, vetoed)
    runtime._persist_intent(run["run_id"], None, active)
    authorization = store.arm_entry_authorization(
        "flat-after-vetoed-planned-auth",
        environment=settings.environment,
        account_id=settings.expected_account_id,
        strategy_date=TRADE_DATE.isoformat(),
        expires_at=NOW + timedelta(hours=1),
        requested_by="test_operator",
        reason="verify broker-attempted chains define reconciliation",
        armed_at=NOW - timedelta(minutes=1),
    )
    store.begin_authorized_entry_submission(
        authorization["authorization_id"],
        environment=settings.environment,
        account_id=settings.expected_account_id,
        strategy_date=TRADE_DATE.isoformat(),
        run_id=run["run_id"],
        intent_id=active.intent_id,
        chain_id=active.chain_id,
        attempt_id=active.attempt_id,
        client_order_id=active.client_order_id,
        request=active.arguments,
        observed_at=NOW,
    )
    store.transition_order_chain(
        active.chain_id, "FILLED", attempt_id=active.attempt_id
    )
    run = store.transition_strategy_run(
        run["run_id"], "SUBMITTING", "TEST_ACTIVE_SUBMITTED"
    )
    run = store.transition_strategy_run(
        run["run_id"], "POSITION_OPEN", "TEST_ACTIVE_FILLED"
    )

    exit_intent = build_exit_from_entry_arguments(
        active.arguments,
        limit_debit=D("1.00"),
        environment=settings.environment,
        account_id=settings.expected_account_id,
        event_date=TRADE_DATE,
        strategy_version="filled-round-trip-exit",
    )
    runtime._persist_intent(run["run_id"], None, exit_intent)
    store.transition_order_chain(exit_intent.chain_id, "SUBMITTING")
    store.record_order_attempt(
        exit_intent.attempt_id,
        chain_id=exit_intent.chain_id,
        sequence=0,
        client_order_id=exit_intent.client_order_id,
        request=exit_intent.arguments,
        broker_order_id="filled-exit-order",
    )
    store.transition_order_chain(
        exit_intent.chain_id, "FILLED", attempt_id=exit_intent.attempt_id
    )
    run = store.transition_strategy_run(
        run["run_id"], "EXIT_SUBMITTING", "TEST_EXIT_SUBMITTED"
    )
    run = store.transition_strategy_run(
        run["run_id"], "EXIT_PENDING", "TEST_EXIT_FILLED"
    )
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_account_activities",
        AsyncMock(
            return_value=SimpleNamespace(
                assignment_or_exercise_detected=False,
                activity_types=(),
            )
        ),
    )
    monkeypatch.setattr(
        runtime.execution,
        "reconcile_exit_order",
        AsyncMock(return_value=None),
    )

    reconciled = await runtime._reconcile_active(run, _snapshot())

    assert store.latest_order_attempt(vetoed.chain_id) is None
    assert reconciled is not None and reconciled["state"] == "FLAT"
    assert store.list_strategy_transitions(run["run_id"])[-1]["reason_code"] == (
        "BROKER_FLAT_RECONCILED"
    )


@pytest.mark.asyncio
async def test_after_close_exit_pending_never_reprices(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    for state in (
        "SCREENING",
        "AI_REVIEW",
        "POLICY_CHECK",
        "SUBMITTING",
        "POSITION_OPEN",
        "EXIT_SUBMITTING",
        "EXIT_PENDING",
    ):
        run = store.transition_strategy_run(run["run_id"], state, "TEST")
    reprice = AsyncMock(side_effect=AssertionError("closed market must not reprice"))
    monkeypatch.setattr(runtime, "_reprice_active_order", reprice)
    after_close = datetime(2026, 9, 3, 20, 5, tzinfo=UTC)
    snapshot = replace(
        _snapshot(observed_at=after_close),
        market_is_open=False,
    )

    result = await runtime._manage_active(
        run,
        snapshot,
        ScheduleAction.EXIT,
        after_close,
    )

    assert result == {"status": "monitoring_exit", "state": "EXIT_PENDING"}
    reprice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["DISCOVERING", "AI_REVIEW", "POLICY_CHECK"])
async def test_pre_dispatch_crash_state_resumes_screening_during_entry_window(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_state: str,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path, f"resume-{crash_state}.sqlite3")
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    if crash_state != "DISCOVERING":
        run = store.transition_strategy_run(run["run_id"], "SCREENING", "TEST")
    if crash_state in {"AI_REVIEW", "POLICY_CHECK"}:
        run = store.transition_strategy_run(run["run_id"], "AI_REVIEW", "TEST")
    if crash_state == "POLICY_CHECK":
        run = store.transition_strategy_run(run["run_id"], "POLICY_CHECK", "TEST")
    scan = AsyncMock(return_value={"status": "screened_after_restart"})
    monkeypatch.setattr(runtime, "_scan_profile_entry", scan)

    result = await runtime._manage_active(
        run,
        _snapshot(),
        ScheduleAction.ENTRY_SCAN,
        NOW,
    )

    assert result == {"status": "screened_after_restart"}
    assert store.get_strategy_run(run["run_id"])["state"] == "SCREENING"
    assert scan.await_args.kwargs["existing_run"]["state"] == "SCREENING"


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["DISCOVERING", "AI_REVIEW", "POLICY_CHECK"])
async def test_pre_dispatch_crash_state_expires_to_no_trade_after_cutoff(
    tmp_path: Path,
    valid_env_file: Path,
    crash_state: str,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path, f"expire-{crash_state}.sqlite3")
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    if crash_state != "DISCOVERING":
        run = store.transition_strategy_run(run["run_id"], "SCREENING", "TEST")
    if crash_state in {"AI_REVIEW", "POLICY_CHECK"}:
        run = store.transition_strategy_run(run["run_id"], "AI_REVIEW", "TEST")
    if crash_state == "POLICY_CHECK":
        run = store.transition_strategy_run(run["run_id"], "POLICY_CHECK", "TEST")
    after_cutoff = datetime(2026, 9, 3, 14, 51, tzinfo=UTC)  # 10:51 ET

    result = await runtime._manage_active(
        run,
        _snapshot(observed_at=after_cutoff),
        ScheduleAction.OBSERVE,
        after_cutoff,
    )

    assert result == {"status": "no_trade", "reason": "ENTRY_WINDOW_EXPIRED"}
    assert store.get_strategy_run(run["run_id"])["state"] == "NO_TRADE"


@pytest.mark.asyncio
async def test_ai_review_restart_marks_interrupted_agent_failed(
    tmp_path: Path,
    valid_env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(valid_env_file)
    store = _store(tmp_path)
    runtime = ThetaTrapRuntime(settings, store, StubConnection())  # type: ignore[arg-type]
    run = runtime._create_intraday_run()
    run = store.transition_strategy_run(run["run_id"], "SCREENING", "TEST")
    run = store.transition_strategy_run(run["run_id"], "AI_REVIEW", "TEST")
    store.start_agent_run(
        "interrupted-agent",
        run_id=run["run_id"],
        model="test-model",
        prompt_hash="prompt-hash",
        config_hash="config-hash",
        started_at=NOW - timedelta(seconds=30),
    )
    monkeypatch.setattr(
        runtime,
        "_scan_profile_entry",
        AsyncMock(return_value={"status": "restarted"}),
    )

    await runtime._manage_active(
        run,
        _snapshot(),
        ScheduleAction.ENTRY_SCAN,
        NOW,
    )

    interrupted = store.latest_agent_run_for_strategy(run["run_id"])
    assert interrupted is not None
    assert interrupted["status"] == "FAILED"
    assert interrupted["error_type"] == "WorkerRestart"
