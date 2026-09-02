"""Autonomous ThetaTrap scheduler and persisted strategy state machine."""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from thetatrap.advisory import (
    AdvisoryAgentFactory,
    run_rejected_candidate_advisory,
)
from thetatrap.agent import AgentContext, AgentOutcome, QwenAgent
from thetatrap.agent_tools import RuntimeAgentTools
from thetatrap.errors import PolicyError
from thetatrap.events import EventConfig, EventDefinition, load_events
from thetatrap.execution import (
    BrokerSnapshot,
    ExecutionResult,
    ExecutionService,
    quote_atomic_exit,
)
from thetatrap.intraday import (
    INTRADAY_PROFILE_ID,
    INTRADAY_STRATEGY_NAME,
    INTRADAY_STRATEGY_VERSION,
    IntradayStrategyConfig,
    evaluate_intraday_candidate_structure,
    evaluate_intraday_symbol,
    rank_intraday_candidates,
)
from thetatrap.market import MarketCollection, collect_symbol_market
from thetatrap.mcp.client import MCPConnection
from thetatrap.orders import (
    OrderIntent,
    build_entry_order_intent,
    build_exit_from_entry_arguments,
    serialize_candidate,
    serialize_evaluation,
    serialize_for_storage,
)
from thetatrap.policy import payload_hash
from thetatrap.schedule import (
    SEP3_INTRADAY_CANARY_SESSION,
    ScheduleAction,
    action_for_time,
    to_market_time,
    verified_events_for_day,
)
from thetatrap.settings import RuntimeSettings, StrategyProfile, account_suffix
from thetatrap.storage import Store
from thetatrap.strategy import StrategyConfig, evaluate_symbol, rank_candidates


LOGGER = logging.getLogger(__name__)
LEASE_NAME = "thetatrap-worker-cycle"
LEASE_TTL_SECONDS = 180
REPRICE_INTERVAL_SECONDS = 30
INTRADAY_AGENT_REVIEW_COOLDOWN_SECONDS = 300
MAX_EXIT_ORDER_CHAINS = 3
WORKING_CHAIN_STATES = frozenset(
    {
        "PLANNED",
        "SUBMITTING",
        "UNKNOWN",
        "PENDING",
        "PARTIALLY_FILLED",
        "CANCEL_PENDING",
        "REPLACEMENT_PENDING",
        "ERROR",
    }
)

AgentFactory = Callable[[RuntimeSettings, RuntimeAgentTools], QwenAgent]


class ThetaTrapRuntime:
    def __init__(
        self,
        settings: RuntimeSettings,
        store: Store,
        connection: MCPConnection,
        *,
        events: EventConfig | None = None,
        strategy_config: StrategyConfig | None = None,
        intraday_config: IntradayStrategyConfig | None = None,
        agent_factory: AgentFactory | None = None,
        advisory_agent_factory: AdvisoryAgentFactory | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.connection = connection
        self.events = events or load_events()
        self.strategy_config = strategy_config or StrategyConfig()
        self.intraday_config = intraday_config or IntradayStrategyConfig()
        self.execution = ExecutionService(settings, store, connection)
        self.owner_id = str(uuid.uuid4())
        self.agent_factory = agent_factory or (
            lambda configured, tools: QwenAgent(configured, tools)
        )
        self.advisory_agent_factory = advisory_agent_factory
        self.config_hash = payload_hash(
            {
                "events": self.events.model_dump(mode="json"),
                "strategy": serialize_for_storage(self.strategy_config),
                "intraday_strategy": _serialize_intraday_config(
                    self.intraday_config
                ),
                "active_strategy_profile": self.settings.strategy_profile.value,
                "market_data_profile": self.settings.market_data_status(),
            }
        )

    async def cycle(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if not self.store.acquire_lease(
            LEASE_NAME, self.owner_id, LEASE_TTL_SECONDS, now=current
        ):
            return {"status": "skipped", "reason": "cycle_lease_held"}
        try:
            return await self._cycle_locked(current)
        finally:
            self.store.release_lease(LEASE_NAME, self.owner_id)

    async def _cycle_locked(self, now: datetime) -> dict[str, Any]:
        active = self.store.find_active_strategy_run(
            environment=self.settings.environment
        )
        snapshot = await self.execution.read_broker_snapshot(
            run_id=active["run_id"] if active else None, now=now
        )
        if snapshot.equity <= Decimal("99000"):
            self.store.activate_kill_switch(
                "equity at or below 99000",
                "worker",
                run_id=active["run_id"] if active else None,
                evidence={"equity": str(snapshot.equity)},
                activated_at=now,
            )
            active = self.store.find_active_strategy_run(
                environment=self.settings.environment
            )

        if active is not None:
            active = await self._reconcile_active(active, snapshot)

        active = self.store.find_active_strategy_run(
            environment=self.settings.environment
        )
        market_day = to_market_time(now).date()
        if (
            active is not None
            and active["state"]
            in {"DISCOVERING", "SCREENING", "AI_REVIEW", "POLICY_CHECK", "ERROR"}
            and datetime.fromisoformat(active["strategy_date"]).date() < market_day
        ):
            self.store.transition_strategy_run(
                active["run_id"],
                "NO_TRADE",
                "STALE_ENTRY_SESSION_CLOSED",
                {"closed_before_profile_scan": self.settings.strategy_profile.value},
            )
            active = None
        open_trade_date = (
            datetime.fromisoformat(active["strategy_date"]).date()
            if active is not None
            and active["state"]
            in {
                "SUBMITTING",
                "ORDER_PENDING",
                "CANCEL_PENDING",
                "POSITION_OPEN",
                "EXIT_SUBMITTING",
                "EXIT_PENDING",
                "RISK_OFF",
            }
            else None
        )
        has_working_entry = bool(
            active
            and active["state"] in {"SUBMITTING", "ORDER_PENDING", "CANCEL_PENDING"}
        )
        schedule_profile = (
            self._run_strategy_profile(active)
            if active is not None
            else self.settings.strategy_profile
        )
        action = action_for_time(
            now=now,
            config=self.events,
            open_trade_date=open_trade_date,
            has_working_entry=has_working_entry,
            strategy_profile=schedule_profile,
            intraday_session=SEP3_INTRADAY_CANARY_SESSION,
        )
        if (
            (
                self.store.get_kill_switch()["kill_switch_enabled"]
                or (active is not None and active["state"] == "RISK_OFF")
            )
            and snapshot.positions
            and snapshot.market_is_open
        ):
            action = ScheduleAction.EXIT

        if active is not None:
            result = await self._manage_active(active, snapshot, action, now)
        elif action is ScheduleAction.ENTRY_SCAN:
            result = await self._scan_profile_entry(snapshot, now)
        elif action is ScheduleAction.FINAL_SNAPSHOT:
            result = {
                "status": "final_snapshot",
                "equity": str(snapshot.equity),
                "positions": len(snapshot.positions),
                "open_orders": len(snapshot.open_orders),
            }
        else:
            result = {"status": "idle", "action": action.value}

        current_run = self.store.find_active_strategy_run(
            environment=self.settings.environment
        )
        detail = {
            **result,
            "environment": self.settings.environment,
            "paper_mode": True,
            "execution_enabled": self.settings.execution_enabled,
            "account_suffix": account_suffix(str(snapshot.account["id"])),
            "market_is_open": snapshot.market_is_open,
            "equity": str(snapshot.equity),
            "position_count": len(snapshot.positions),
            "open_order_count": len(snapshot.open_orders),
            "strategy_state": current_run["state"] if current_run else None,
            "strategy_profile": self.settings.strategy_profile.value,
            "market_data_profile": self.settings.market_data_status(),
            "required_schema_hash": self.connection.registry.required_schema_hash,
        }
        self.store.record_heartbeat(
            status=(
                "risk_off"
                if self.store.get_kill_switch()["kill_switch_enabled"]
                or (current_run and current_run["state"] == "RISK_OFF")
                else "healthy"
            ),
            environment=self.settings.environment,
            account_suffix=detail["account_suffix"],
            mcp_schema_hash=self.connection.registry.required_schema_hash,
            market_is_open=snapshot.market_is_open,
            detail=detail,
        )
        return detail

    async def _reconcile_active(
        self, run: dict[str, Any], snapshot: BrokerSnapshot
    ) -> dict[str, Any] | None:
        run_id = run["run_id"]
        state = run["state"]
        entry_fill_reconciled_from_order = False
        activities = await self.execution.reconcile_account_activities(
            run_id=run_id,
            after=datetime.fromisoformat(run["strategy_date"] + "T00:00:00+00:00"),
            now=snapshot.observed_at,
        )
        if activities.assignment_or_exercise_detected:
            self.store.activate_kill_switch(
                "option assignment, exercise, or corporate action detected",
                "worker",
                run_id=run_id,
                evidence={"activity_types": list(activities.activity_types)},
                activated_at=snapshot.observed_at,
            )
            run = self.store.get_strategy_run(run_id) or run
            state = run["state"]
        if snapshot.positions:
            intact, evidence = self._intact_entry_position(run, snapshot)
            if not intact:
                self.store.activate_kill_switch(
                    "assignment or unmatched option position detected",
                    "worker",
                    run_id=run_id,
                    evidence=evidence,
                    activated_at=snapshot.observed_at,
                )
                run = self.store.get_strategy_run(run_id) or run
                state = run["state"]
        entry_chain: dict[str, Any] | None = None
        if state in {"SUBMITTING", "ORDER_PENDING", "CANCEL_PENDING", "RISK_OFF"}:
            entry_chain = self._active_entry_chain(run)
            if entry_chain is not None and entry_chain["state"] not in {
                "CANCELED",
                "FILLED",
                "REJECTED",
                "EXPIRED",
            }:
                attempt = self.store.latest_order_attempt(entry_chain["chain_id"])
                if attempt is not None:
                    reconciled_entry = await self.execution.reconcile_entry_order(
                        run_id=run_id,
                        chain_id=entry_chain["chain_id"],
                        attempt_id=attempt["attempt_id"],
                        client_order_id=attempt["client_order_id"],
                    )
                    entry_fill_reconciled_from_order = bool(
                        reconciled_entry is not None
                        and reconciled_entry.broker_status == "filled"
                    )
                    run = self.store.get_strategy_run(run_id) or run
                    state = run["state"]

        if (
            state in {"SUBMITTING", "ORDER_PENDING", "CANCEL_PENDING"}
            and entry_chain is not None
            and entry_chain["state"] in {"CANCELED", "REJECTED", "EXPIRED"}
            and not snapshot.positions
            and not snapshot.open_orders
        ):
            if state == "ORDER_PENDING":
                run = self.store.transition_strategy_run(
                    run_id,
                    "CANCEL_PENDING",
                    "ENTRY_TERMINAL_RECONCILED",
                    {"chain_state": entry_chain["state"]},
                )
            run = self.store.transition_strategy_run(
                run_id,
                "NO_TRADE",
                "ENTRY_TERMINAL_ZERO_FILL_RECONCILED",
                {"chain_state": entry_chain["state"]},
            )
            state = run["state"]

        if state in {"EXIT_SUBMITTING", "EXIT_PENDING", "RISK_OFF"}:
            chains = self.store.list_order_chains(run_id, purpose="exit")
            if chains:
                attempt = self.store.latest_order_attempt(chains[-1]["chain_id"])
                if attempt is not None:
                    await self.execution.reconcile_exit_order(
                        run_id=run_id,
                        chain_id=chains[-1]["chain_id"],
                        attempt_id=attempt["attempt_id"],
                        client_order_id=attempt["client_order_id"],
                    )
                    run = self.store.get_strategy_run(run_id) or run
                    state = run["state"]

        if snapshot.positions and state in {
            "SUBMITTING",
            "ORDER_PENDING",
            "CANCEL_PENDING",
        }:
            run = self.store.transition_strategy_run(
                run_id,
                "POSITION_OPEN",
                "BROKER_POSITION_RECONCILED",
                {"position_count": len(snapshot.positions)},
            )
        elif (
            not snapshot.positions
            and not snapshot.open_orders
            and not entry_fill_reconciled_from_order
            and self._all_order_chains_terminal(run_id)
            and state
            in {"POSITION_OPEN", "EXIT_SUBMITTING", "EXIT_PENDING", "RISK_OFF"}
        ):
            run = self.store.transition_strategy_run(
                run_id, "FLAT", "BROKER_FLAT_RECONCILED", {}
            )
        return run

    def _all_order_chains_terminal(self, run_id: str) -> bool:
        terminal = {"CANCELED", "FILLED", "REJECTED", "EXPIRED"}
        attempted_chains = [
            chain
            for chain in self.store.list_order_chains(run_id)
            if self.store.latest_order_attempt(str(chain["chain_id"])) is not None
        ]
        # Screening persists immutable PLANNED chains even when Qwen vetoes a
        # candidate. Those chains never reached Alpaca and therefore cannot
        # represent unresolved broker state. Conversely, a run with no durable
        # broker attempt must never be inferred flat from an empty snapshot.
        if not attempted_chains or not all(
            str(chain["state"]) in terminal for chain in attempted_chains
        ):
            return False
        filled_entry_exists = any(
            chain["purpose"] == "entry" and chain["state"] == "FILLED"
            for chain in attempted_chains
        )
        if not filled_entry_exists:
            return True
        # A temporarily stale empty position snapshot after an entry fill is
        # not proof of flatness. Once exposure was durably established, require
        # evidence that an exit order actually reached Alpaca before allowing a
        # later empty broker snapshot to close the run.
        return any(
            chain["purpose"] == "exit" and chain["state"] == "FILLED"
            for chain in attempted_chains
        )

    async def _manage_active(
        self,
        run: dict[str, Any],
        snapshot: BrokerSnapshot,
        action: ScheduleAction,
        now: datetime,
    ) -> dict[str, Any]:
        run = self.store.get_strategy_run(run["run_id"]) or run
        state = run["state"]
        if snapshot.positions:
            intact, evidence = self._intact_entry_position(run, snapshot)
            if not intact:
                self.store.activate_kill_switch(
                    "assignment or unmatched option position detected",
                    "worker",
                    run_id=run["run_id"],
                    evidence=evidence,
                    activated_at=now,
                )
                return {
                    "status": "risk_off",
                    "reason": "ASSIGNMENT_OR_UNMATCHED_LEGS",
                    "manual_broker_intervention_required": True,
                }
        if state in {"DISCOVERING", "SCREENING", "AI_REVIEW", "POLICY_CHECK"}:
            authorization = self.store.get_entry_authorization(
                self.settings.environment,
                self.settings.expected_account_id,
                run["strategy_date"],
            )
            has_consumed_authorization = bool(
                authorization and authorization["state"] == "CONSUMED"
            )
            has_entry_attempt = any(
                self.store.latest_order_attempt(chain["chain_id"]) is not None
                for chain in self.store.list_order_chains(
                    run["run_id"], purpose="entry"
                )
            )
            if has_consumed_authorization or has_entry_attempt:
                self.store.activate_kill_switch(
                    "pre-dispatch strategy state conflicts with broker-capable records",
                    "worker",
                    run_id=run["run_id"],
                    evidence={
                        "strategy_state": state,
                        "has_consumed_authorization": has_consumed_authorization,
                        "has_entry_attempt": has_entry_attempt,
                    },
                    activated_at=now,
                )
                return {
                    "status": "risk_off",
                    "reason": "PRE_DISPATCH_STATE_CONFLICT",
                    "manual_broker_intervention_required": True,
                }
            latest_agent = self.store.latest_agent_run_for_strategy(run["run_id"])
            if latest_agent is not None and latest_agent["status"] == "STARTED":
                self.store.finish_agent_run(
                    latest_agent["agent_run_id"],
                    "FAILED",
                    result={"reason": "WORKER_RESTART_RECOVERY"},
                    error_type="WorkerRestart",
                    ended_at=now,
                )
            if to_market_time(now).time() >= self._cancel_unfilled_time(run):
                self.store.transition_strategy_run(
                    run["run_id"],
                    "NO_TRADE",
                    "ENTRY_WINDOW_EXPIRED",
                    {"recovered_from_state": state},
                )
                return {"status": "no_trade", "reason": "ENTRY_WINDOW_EXPIRED"}
            if action is ScheduleAction.ENTRY_SCAN:
                if state != "SCREENING":
                    run = self.store.transition_strategy_run(
                        run["run_id"],
                        "SCREENING",
                        "PRE_DISPATCH_RESTART_RECOVERY",
                        {"recovered_from_state": state},
                    )
                return await self._scan_profile_entry(
                    snapshot,
                    now,
                    existing_run=run,
                )
            return {"status": "monitoring", "state": state, "action": action.value}
        if state == "RISK_OFF":
            entry_chain, entry_attempt = self._working_chain_attempt(
                run["run_id"], purpose="entry"
            )
            exit_chain, exit_attempt = self._working_chain_attempt(
                run["run_id"], purpose="exit"
            )
            entry_is_open = entry_chain is not None and self._attempt_is_open(
                entry_attempt, snapshot
            )
            exit_is_open = exit_chain is not None and self._attempt_is_open(
                exit_attempt, snapshot
            )
            if entry_is_open:
                return await self._cancel_active_entry(run)
            if exit_is_open:
                if snapshot.positions and snapshot.market_is_open:
                    return await self._reprice_active_order(
                        run, purpose="exit", now=now
                    )
                return {"status": "risk_off", "state": state}
            if snapshot.open_orders:
                return {
                    "status": "risk_off",
                    "reason": "UNRECONCILED_OPEN_ORDER",
                    "manual_broker_intervention_required": True,
                }
            if snapshot.positions and snapshot.market_is_open:
                return await self._start_exit(run, snapshot, now)
            return {"status": "risk_off", "state": state}
        if state in {"SUBMITTING", "ORDER_PENDING", "CANCEL_PENDING"}:
            if action is ScheduleAction.CANCEL_UNFILLED:
                return await self._cancel_active_entry(run)
            if action is ScheduleAction.ENTRY_SCAN and snapshot.market_is_open:
                return await self._reprice_active_order(run, purpose="entry", now=now)
            return {"status": "monitoring_entry", "state": state}
        if state == "POSITION_OPEN":
            if action is ScheduleAction.EXIT and snapshot.market_is_open:
                return await self._start_exit(run, snapshot, now)
            return {"status": "position_open", "state": state}
        if state in {"EXIT_SUBMITTING", "EXIT_PENDING"}:
            if action is ScheduleAction.EXIT and snapshot.market_is_open:
                return await self._reprice_active_order(run, purpose="exit", now=now)
            return {"status": "monitoring_exit", "state": state}
        return {"status": "monitoring", "state": state, "action": action.value}

    def _working_chain_attempt(
        self, run_id: str, *, purpose: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        run = self.store.get_strategy_run(run_id)
        if purpose == "entry" and run is not None:
            chain = self._active_entry_chain(run)
        else:
            chains = self.store.list_order_chains(run_id, purpose=purpose)
            chain = chains[-1] if chains else None
        if chain is None or chain["state"] not in WORKING_CHAIN_STATES:
            return None, None
        return chain, self.store.latest_order_attempt(chain["chain_id"])

    @staticmethod
    def _attempt_is_open(
        attempt: dict[str, Any] | None, snapshot: BrokerSnapshot
    ) -> bool:
        if attempt is None:
            return False
        identifiers = {
            str(value)
            for value in (
                attempt.get("broker_order_id"),
                attempt.get("client_order_id"),
            )
            if value
        }
        return any(
            identifiers
            & {
                str(value)
                for value in (order.get("id"), order.get("client_order_id"))
                if value
            }
            for order in snapshot.open_orders
        )

    def _intact_entry_position(
        self, run: dict[str, Any], snapshot: BrokerSnapshot
    ) -> tuple[bool, dict[str, Any]]:
        entry = self._active_entry_intent(run)
        if entry is None:
            return False, {"reason": "ENTRY_INTENT_MISSING"}
        payload = entry["payload"]
        try:
            strategy_quantity = Decimal(str(payload["qty"]))
            expected = {
                str(leg["symbol"]): (
                    Decimal(str(leg["ratio_qty"]))
                    * strategy_quantity
                    * (Decimal("1") if leg["side"] == "buy" else Decimal("-1"))
                )
                for leg in payload["legs"]
            }
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False, {"reason": "ENTRY_INTENT_POSITION_MAP_INVALID"}
        if len(expected) != 4:
            return False, {
                "reason": "ENTRY_INTENT_NOT_FOUR_UNIQUE_LEGS",
                "expected_leg_count": len(expected),
            }

        observed: dict[str, Decimal] = {}
        for position in snapshot.positions:
            asset_class = str(position.get("asset_class") or "").lower()
            symbol = str(position.get("symbol") or "")
            if asset_class not in {"us_option", "option"}:
                return False, {
                    "reason": "NON_OPTION_POSITION",
                    "observed_symbol": symbol,
                }
            if not symbol or symbol in observed:
                return False, {"reason": "INVALID_OR_DUPLICATE_POSITION_SYMBOL"}
            try:
                quantity = Decimal(str(position["qty"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return False, {
                    "reason": "POSITION_QUANTITY_INVALID",
                    "observed_symbol": symbol,
                }
            side = str(position.get("side") or "").lower()
            if side == "long":
                quantity = abs(quantity)
            elif side == "short":
                quantity = -abs(quantity)
            elif side:
                return False, {
                    "reason": "POSITION_SIDE_INVALID",
                    "observed_symbol": symbol,
                }
            observed[symbol] = quantity

        if observed != expected:
            return False, {
                "reason": "SIGNED_POSITION_MISMATCH",
                "expected": {
                    symbol: str(qty) for symbol, qty in sorted(expected.items())
                },
                "observed": {
                    symbol: str(qty) for symbol, qty in sorted(observed.items())
                },
            }
        return True, {
            "expected_leg_count": 4,
            "observed_leg_count": 4,
            "signed_quantities_match": True,
        }

    async def _scan_profile_entry(
        self,
        snapshot: BrokerSnapshot,
        now: datetime,
        *,
        existing_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = (
            self._run_strategy_profile(existing_run)
            if existing_run is not None
            else self.settings.strategy_profile
        )
        if profile is StrategyProfile.INTRADAY_CANARY:
            return await self._scan_intraday_entry(
                snapshot,
                now,
                existing_run=existing_run,
            )
        return await self._scan_entry(snapshot, now, existing_run=existing_run)

    async def _scan_intraday_entry(
        self,
        snapshot: BrokerSnapshot,
        now: datetime,
        *,
        existing_run: dict[str, Any] | None = None,
        strategy_date_override: date | None = None,
    ) -> dict[str, Any]:
        """Screen the separately labelled Sep 3 intraday contingency."""

        market_day = to_market_time(now).date()
        requested_day = strategy_date_override or market_day
        if requested_day != self.intraday_config.trade_date:
            return {"status": "idle", "reason": "OUTSIDE_INTRADAY_CANARY_DATE"}
        run = existing_run or self._create_intraday_run()
        if run["state"] == "DISCOVERING":
            run = self.store.transition_strategy_run(
                run["run_id"],
                "SCREENING",
                "INTRADAY_ENTRY_WINDOW_OPEN",
                {"strategy_profile": StrategyProfile.INTRADAY_CANARY.value},
            )

        eligible: list[tuple[Any, MarketCollection]] = []
        rejected = 0
        collection_errors: list[dict[str, str]] = []
        for symbol in self.intraday_config.allowed_symbols:
            try:
                collection = await collect_symbol_market(
                    self.connection,
                    symbol=symbol,
                    trade_expiration=self.intraday_config.expiration,
                    term_expiration=self.intraday_config.expiration,
                    stock_feed=self.settings.alpaca_stock_feed,
                    option_feed=self.settings.alpaca_option_feed,
                )
                self._persist_collection(run["run_id"], collection)
                evaluation = evaluate_intraday_symbol(
                    symbol=symbol,
                    observed_at=collection.collected_at,
                    underlying=collection.underlying,
                    option_chain=collection.front_chain,
                    buying_power=snapshot.buying_power,
                    config=self.intraday_config,
                )
                if evaluation.candidate is None:
                    rejected += 1
                    self._persist_evaluation(
                        run["run_id"], collection, evaluation, rank=None
                    )
                else:
                    eligible.append((evaluation.candidate, collection))
            except Exception as exc:
                LOGGER.exception("intraday candidate collection failed for %s", symbol)
                collection_errors.append(
                    {"symbol": symbol, "error_type": type(exc).__name__}
                )

        if not eligible:
            advisory = await run_rejected_candidate_advisory(
                self.settings,
                self.store,
                self.connection,
                run_id=run["run_id"],
                events=[
                    self._intraday_strategy_context(symbol)
                    for symbol in self.intraday_config.allowed_symbols
                ],
                now=now,
                agent_factory=self.advisory_agent_factory,
            )
            return {
                "status": "screening",
                "strategy_profile": StrategyProfile.INTRADAY_CANARY.value,
                "eligible_candidates": 0,
                "rejected_candidates": rejected,
                "collection_errors": collection_errors,
                "advisory": advisory,
            }

        ranked = rank_intraday_candidates(item[0] for item in eligible)
        by_symbol = {item[0].symbol: item[1] for item in eligible}
        existing_ranks = [
            int(item["candidate_rank"])
            for item in self.store.list_candidates(run["run_id"])
            if item.get("candidate_rank") is not None
        ]
        rank_offset = max(existing_ranks, default=0)
        for rank, candidate in enumerate(ranked, start=1):
            self._persist_candidate(
                run["run_id"],
                by_symbol[candidate.symbol],
                candidate,
                rank=rank_offset + rank,
            )

        latest_agent = self.store.latest_agent_run_for_strategy(run["run_id"])
        if latest_agent is not None:
            latest_started = datetime.fromisoformat(str(latest_agent["started_at"]))
            elapsed = now.astimezone(UTC) - latest_started.astimezone(UTC)
            if elapsed < timedelta(seconds=INTRADAY_AGENT_REVIEW_COOLDOWN_SECONDS):
                return {
                    "status": "screening",
                    "reason": "QWEN_REVIEW_COOLDOWN",
                    "eligible_candidates": len(ranked),
                    "next_review_at": (
                        latest_started
                        + timedelta(seconds=INTRADAY_AGENT_REVIEW_COOLDOWN_SECONDS)
                    ).isoformat(),
                }

        for index, candidate in enumerate(ranked):
            collection = by_symbol[candidate.symbol]
            candidate_id = _candidate_id(run["run_id"], collection)
            if self.store.get_strategy_run(run["run_id"])["state"] == "SCREENING":
                self.store.transition_strategy_run(
                    run["run_id"],
                    "AI_REVIEW",
                    "INTRADAY_CANDIDATE_ELIGIBLE",
                    {"candidate_id": candidate_id, "rank": index + 1},
                )
            outcome = await self._review_candidate(
                run,
                snapshot,
                candidate,
                candidate_id,
                self._intraday_strategy_context(candidate.symbol),
                now,
            )
            if outcome is not None:
                return outcome
            self._return_run_to_screening(
                run["run_id"],
                "INTRADAY_REVIEW_CYCLE_CONTINUES",
                {"candidate_id": candidate_id, "rank": index + 1},
            )
            if index < len(ranked) - 1:
                continue
            else:
                return {
                    "status": "screening",
                    "reason": "NO_CANDIDATE_AUTHORIZED_THIS_CYCLE",
                    "eligible_candidates": len(ranked),
                }
        return {
            "status": "screening",
            "reason": "NO_CANDIDATE_AUTHORIZED_THIS_CYCLE",
        }

    async def _scan_entry(
        self,
        snapshot: BrokerSnapshot,
        now: datetime,
        *,
        existing_run: dict[str, Any] | None = None,
        events_override: tuple[EventDefinition, ...] | None = None,
        strategy_date_override: date | None = None,
    ) -> dict[str, Any]:
        market_day = to_market_time(now).date()
        events = events_override or verified_events_for_day(self.events, market_day)
        if not events:
            return {"status": "idle", "reason": "NO_VERIFIED_EVENTS"}
        run = existing_run or self._create_run(
            strategy_date_override or market_day, events
        )
        if run["state"] == "DISCOVERING":
            run = self.store.transition_strategy_run(
                run["run_id"], "SCREENING", "ENTRY_WINDOW_OPEN", {}
            )

        eligible: list[tuple[Any, MarketCollection, EventDefinition]] = []
        rejected = 0
        collection_errors: list[dict[str, str]] = []
        for event in events:
            try:
                collection = await collect_symbol_market(
                    self.connection,
                    symbol=event.symbol,
                    trade_expiration=self.events.trade_expiration,
                    term_expiration=self.events.term_expiration,
                    stock_feed=self.settings.alpaca_stock_feed,
                    option_feed=self.settings.alpaca_option_feed,
                )
                self._persist_collection(run["run_id"], collection)
                evaluation = evaluate_symbol(
                    symbol=event.symbol,
                    observed_at=collection.collected_at,
                    underlying=collection.underlying,
                    front_chain=collection.front_chain,
                    back_chain=collection.back_chain,
                    trade_expiration=self.events.trade_expiration,
                    term_expiration=self.events.term_expiration,
                    previous_trading_day=collection.previous_trading_day,
                    initial_equity=snapshot.equity,
                    buying_power=snapshot.buying_power,
                    config=self.strategy_config,
                )
                if evaluation.candidate is None:
                    rejected += 1
                    self._persist_evaluation(
                        run["run_id"], collection, evaluation, rank=None
                    )
                else:
                    eligible.append((evaluation.candidate, collection, event))
            except Exception as exc:
                collection_errors.append(
                    {"symbol": event.symbol, "error_type": type(exc).__name__}
                )

        if not eligible:
            advisory = await run_rejected_candidate_advisory(
                self.settings,
                self.store,
                self.connection,
                run_id=run["run_id"],
                events=[event.model_dump(mode="json") for event in events],
                now=now,
                agent_factory=self.advisory_agent_factory,
            )
            return {
                "status": "screening",
                "eligible_candidates": 0,
                "rejected_candidates": rejected,
                "collection_errors": collection_errors,
                "advisory": advisory,
            }

        ranked = rank_candidates(item[0] for item in eligible)
        by_symbol = {item[0].symbol: (item[1], item[2]) for item in eligible}
        for rank, candidate in enumerate(ranked, start=1):
            collection, _ = by_symbol[candidate.symbol]
            self._persist_candidate(run["run_id"], collection, candidate, rank=rank)

        for index, candidate in enumerate(ranked):
            collection, event = by_symbol[candidate.symbol]
            candidate_id = _candidate_id(run["run_id"], collection)
            if self.store.get_strategy_run(run["run_id"])["state"] == "SCREENING":
                self.store.transition_strategy_run(
                    run["run_id"],
                    "AI_REVIEW",
                    "CANDIDATE_ELIGIBLE",
                    {"candidate_id": candidate_id, "rank": index + 1},
                )
            outcome = await self._review_candidate(
                run, snapshot, candidate, candidate_id, event, now
            )
            if outcome is not None:
                return outcome
            if index < len(ranked) - 1:
                self.store.transition_strategy_run(
                    run["run_id"], "SCREENING", "TRY_NEXT_CANDIDATE", {}
                )
            else:
                self.store.transition_strategy_run(
                    run["run_id"], "NO_TRADE", "ALL_CANDIDATES_REJECTED", {}
                )
        return {"status": "no_trade", "reason": "ALL_CANDIDATES_REJECTED"}

    async def rehearse_entry(
        self,
        snapshot: BrokerSnapshot,
        now: datetime,
        *,
        strategy_date: date,
    ) -> dict[str, Any]:
        """Exercise the real entry pipeline in a broker-mutation-proof replay role.

        The caller must provide a disposable replay store.  Live timestamps are
        retained for every quote-freshness gate while the requested future event
        set is selected explicitly.  This method never bypasses a strategy gate.
        """

        if (
            self.settings.environment != "replay"
            or not self.settings.read_only
            or self.settings.execution_enabled
        ):
            raise PolicyError("decision rehearsal requires the disarmed replay role")
        if not snapshot.market_is_open:
            raise PolicyError("decision rehearsal requires an open market")
        if snapshot.positions or snapshot.open_orders:
            raise PolicyError(
                "decision rehearsal requires a flat account with no open orders"
            )
        events = verified_events_for_day(self.events, strategy_date)
        if not events:
            raise PolicyError(
                "decision rehearsal date has no verified event in frozen configuration"
            )
        return await self._scan_entry(
            snapshot,
            now,
            events_override=events,
            strategy_date_override=strategy_date,
        )

    async def rehearse_intraday_entry(
        self,
        snapshot: BrokerSnapshot,
        now: datetime,
        *,
        strategy_date: date,
    ) -> dict[str, Any]:
        """Exercise the canary decision path while blocking broker mutation."""

        if (
            self.settings.environment != "replay"
            or not self.settings.read_only
            or self.settings.execution_enabled
        ):
            raise PolicyError("decision rehearsal requires the disarmed replay role")
        if self.settings.strategy_profile is not StrategyProfile.INTRADAY_CANARY:
            raise PolicyError("intraday rehearsal requires the intraday_canary profile")
        if strategy_date != self.intraday_config.trade_date:
            raise PolicyError("intraday rehearsal is frozen to Sep 3, 2026")
        if not snapshot.market_is_open:
            raise PolicyError("decision rehearsal requires an open market")
        if snapshot.positions or snapshot.open_orders:
            raise PolicyError(
                "decision rehearsal requires a flat account with no open orders"
            )
        return await self._scan_intraday_entry(
            snapshot,
            now,
            strategy_date_override=strategy_date,
        )

    async def _review_candidate(
        self,
        run: dict[str, Any],
        snapshot: BrokerSnapshot,
        candidate: Any,
        candidate_id: str,
        event: EventDefinition | dict[str, Any],
        now: datetime,
    ) -> dict[str, Any] | None:
        profile = self._run_strategy_profile(run)
        if profile is StrategyProfile.INTRADAY_CANARY:
            if not isinstance(event, dict):
                raise PolicyError("intraday review requires bounded strategy context")
            strategy_context = dict(event)
            strategy_date = self.intraday_config.trade_date
            # Each observation gets its own immutable pre-submission intent.
            # The daily authorization still permits only one broker submission.
            strategy_version = (
                f"{INTRADAY_STRATEGY_VERSION}-"
                f"{hashlib.sha256(candidate_id.encode('utf-8')).hexdigest()[:12]}"
            )
            verified_events: list[dict[str, Any]] = []
        else:
            if not isinstance(event, EventDefinition):
                raise PolicyError("earnings review requires a verified event")
            strategy_context = event.model_dump(mode="json")
            strategy_date = event.event_date
            strategy_version = self.events.strategy_version
            verified_events = [
                item.model_dump(mode="json")
                for item in self.events.events
                if item.status == "verified"
            ]
        intent = build_entry_order_intent(
            candidate,
            environment=self.settings.environment,
            account_id=str(snapshot.account["id"]),
            event_date=strategy_date,
            strategy_version=strategy_version,
        )
        self._persist_intent(run["run_id"], candidate_id, intent)
        context = AgentContext(
            symbol=candidate.symbol,
            event=strategy_context,
            candidate=serialize_candidate(candidate),
            run_summary={
                "run_id": run["run_id"],
                "state": "AI_REVIEW",
                "environment": self.settings.environment,
                "paper_only": True,
                "execution_enabled": self.settings.execution_enabled,
                "market_data_profile": self.settings.market_data_status(),
                "strategy_profile": profile.value,
            },
            order_intent_id=intent.intent_id,
            order_arguments=intent.arguments,
        )
        tools = RuntimeAgentTools(
            self.connection,
            context,
            verified_events=verified_events,
        )
        agent_run_id = (
            "tt-agent-"
            + hashlib.sha256(
                f"{run['run_id']}|{candidate_id}".encode("utf-8")
            ).hexdigest()[:32]
        )
        agent_config_hash = payload_hash(
            {
                "primary": self.settings.featherless_primary_model,
                "fallback": self.settings.featherless_fallback_model,
                "candidate_id": candidate_id,
            }
        )
        self.store.start_agent_run(
            agent_run_id,
            run_id=run["run_id"],
            candidate_id=candidate_id,
            model=(
                self.settings.featherless_primary_model
                + "|fallback="
                + self.settings.featherless_fallback_model
            ),
            prompt_hash=payload_hash(serialize_for_storage(context)),
            config_hash=agent_config_hash,
            started_at=now,
        )
        try:
            decision = await self.agent_factory(self.settings, tools).review(context)
        except Exception as exc:
            self._persist_agent_tools(agent_run_id, tools, now)
            self.store.finish_agent_run(
                agent_run_id,
                "FAILED",
                result={"outcome": "ERROR"},
                error_type=type(exc).__name__,
                ended_at=now,
            )
            LOGGER.exception("candidate agent review failed")
            return None

        self._persist_agent_tools(agent_run_id, tools, now)
        if decision.outcome is AgentOutcome.ALLOW:
            self.store.record_agent_tool_call(
                agent_run_id,
                len(tools.calls),
                principal="qwen",
                tool_name="place_option_order",
                arguments=decision.mutation_arguments or {},
                result={"gateway": "exact_intent_match", "dispatched": False},
                status="authorized_for_fresh_policy_check",
                duration_ms=0,
                is_official_mcp=True,
                called_at=now,
            )
            self.store.finish_agent_run(
                agent_run_id,
                "COMPLETED",
                result=serialize_for_storage(decision),
                ended_at=now,
            )
            self.store.transition_strategy_run(
                run["run_id"],
                "POLICY_CHECK",
                "QWEN_EXACT_TOOL_ALLOW",
                {"agent_run_id": agent_run_id, "model": decision.model},
            )
            try:
                fresh = await self._fresh_revalidate(
                    run, snapshot, candidate, event, intent, now
                )
            except Exception as exc:
                current = self.store.get_strategy_run(run["run_id"])
                if current is not None and current["state"] == "POLICY_CHECK":
                    self.store.transition_strategy_run(
                        run["run_id"],
                        "SCREENING",
                        "FRESH_POLICY_CHECK_ERROR",
                        {"error_type": type(exc).__name__},
                    )
                LOGGER.exception("fresh candidate policy check failed")
                return None
            if not fresh:
                self.store.transition_strategy_run(
                    run["run_id"], "SCREENING", "FRESH_POLICY_REJECTED", {}
                )
                return None
            if not self.settings.execution_enabled:
                self.store.transition_strategy_run(
                    run["run_id"],
                    "NO_TRADE",
                    "SHADOW_ALLOW_EXECUTION_DISARMED",
                    {"intent_id": intent.intent_id},
                )
                return {
                    "status": "shadow_allow",
                    "executed": False,
                    "candidate": candidate.symbol,
                }
            try:
                result = await self.execution.submit_entry(
                    run_id=run["run_id"],
                    intent_id=intent.intent_id,
                    chain_id=intent.chain_id,
                    attempt_id=intent.attempt_id,
                    arguments=intent.arguments,
                )
            except Exception as exc:
                current = self.store.get_strategy_run(run["run_id"])
                if current is not None and current["state"] == "POLICY_CHECK":
                    self.store.transition_strategy_run(
                        run["run_id"],
                        "SCREENING",
                        "ENTRY_PRE_DISPATCH_REJECTED",
                        {"error_type": type(exc).__name__},
                    )
                    LOGGER.warning(
                        "entry rejected before broker dispatch",
                        exc_info=True,
                    )
                    return None
                self.store.activate_kill_switch(
                    "entry submission failed after durable state changed",
                    "worker",
                    run_id=run["run_id"],
                    evidence={"error_type": type(exc).__name__},
                    activated_at=now,
                )
                LOGGER.exception("entry submission failed after state change")
                return {
                    "status": "risk_off",
                    "reason": "ENTRY_SUBMISSION_STATE_UNCERTAIN",
                }
            return _result_dict("entry", result)

        terminal = (
            "VETOED"
            if decision.outcome
            in {AgentOutcome.VETO, AgentOutcome.INSUFFICIENT_EVIDENCE}
            else "FAILED"
        )
        self.store.finish_agent_run(
            agent_run_id,
            terminal,
            result=serialize_for_storage(decision),
            veto_reason=decision.reason_code if terminal == "VETOED" else None,
            error_type=decision.reason_code if terminal == "FAILED" else None,
            ended_at=now,
        )
        return None

    async def _fresh_revalidate(
        self,
        run: dict[str, Any],
        snapshot: BrokerSnapshot,
        original: Any,
        event: EventDefinition | dict[str, Any],
        intent: OrderIntent,
        now: datetime,
    ) -> bool:
        if self._run_strategy_profile(run) is StrategyProfile.INTRADAY_CANARY:
            return await self._fresh_revalidate_intraday(
                run,
                snapshot,
                original,
                intent,
            )
        if not isinstance(event, EventDefinition):
            raise PolicyError("earnings revalidation requires a verified event")
        collection = await collect_symbol_market(
            self.connection,
            symbol=event.symbol,
            trade_expiration=self.events.trade_expiration,
            term_expiration=self.events.term_expiration,
        )
        self._persist_collection(run["run_id"], collection)
        evaluation = evaluate_symbol(
            symbol=event.symbol,
            observed_at=collection.collected_at,
            underlying=collection.underlying,
            front_chain=collection.front_chain,
            back_chain=collection.back_chain,
            trade_expiration=self.events.trade_expiration,
            term_expiration=self.events.term_expiration,
            previous_trading_day=collection.previous_trading_day,
            initial_equity=snapshot.equity,
            buying_power=snapshot.buying_power,
            config=self.strategy_config,
        )
        if evaluation.candidate is None:
            self._persist_evaluation(run["run_id"], collection, evaluation, rank=None)
            return False
        fresh = evaluation.candidate
        original_symbols = [leg.snapshot.contract.symbol for leg in original.legs]
        fresh_symbols = [leg.snapshot.contract.symbol for leg in fresh.legs]
        credit = -Decimal(str(intent.arguments["limit_price"]))
        return (
            original_symbols == fresh_symbols
            and fresh.natural_credit <= credit <= fresh.midpoint_credit
            and (fresh.wing_width - credit) * Decimal("100")
            <= min(Decimal("500"), snapshot.equity * Decimal("0.005"))
        )

    async def _fresh_revalidate_intraday(
        self,
        run: dict[str, Any],
        snapshot: BrokerSnapshot,
        original: Any,
        intent: OrderIntent,
    ) -> bool:
        collection = await collect_symbol_market(
            self.connection,
            symbol=original.symbol,
            trade_expiration=self.intraday_config.expiration,
            term_expiration=self.intraday_config.expiration,
            stock_feed=self.settings.alpaca_stock_feed,
            option_feed=self.settings.alpaca_option_feed,
        )
        self._persist_collection(run["run_id"], collection)
        evaluation = evaluate_intraday_candidate_structure(
            original=original,
            observed_at=collection.collected_at,
            underlying=collection.underlying,
            option_chain=collection.front_chain,
            buying_power=snapshot.buying_power,
            config=self.intraday_config,
        )
        if evaluation.candidate is None:
            self._persist_evaluation(run["run_id"], collection, evaluation, rank=None)
            return False
        fresh = evaluation.candidate
        original_symbols = [leg.snapshot.contract.symbol for leg in original.legs]
        fresh_symbols = [leg.snapshot.contract.symbol for leg in fresh.legs]
        credit = -Decimal(str(intent.arguments["limit_price"]))
        maximum_loss = (self.intraday_config.wing_width - credit) * Decimal("100")
        return (
            original_symbols == fresh_symbols
            and self.intraday_config.minimum_proposed_credit
            <= credit
            <= fresh.midpoint_credit
            and maximum_loss <= self.intraday_config.maximum_loss_dollars
            and maximum_loss <= snapshot.buying_power
        )

    async def _cancel_active_entry(self, run: dict[str, Any]) -> dict[str, Any]:
        chain = self._active_entry_chain(run)
        if chain is None:
            self.store.transition_strategy_run(
                run["run_id"], "NO_TRADE", "NO_ENTRY_ORDER_TO_CANCEL", {}
            )
            return {"status": "no_trade", "reason": "NO_ENTRY_ORDER_TO_CANCEL"}
        attempt = self.store.latest_order_attempt(chain["chain_id"])
        intent = self.store.get_order_intent(str(chain["intent_id"]))
        broker_order_id = (
            attempt.get("broker_order_id")
            if attempt is not None
            else None
        ) or (
            attempt.get("request", {}).get("order_id")
            if attempt is not None
            else None
        )
        if not attempt or not broker_order_id or intent is None:
            self.store.activate_kill_switch(
                "working entry could not be identified for cancellation",
                "worker",
                run_id=run["run_id"],
            )
            return {"status": "risk_off", "reason": "ENTRY_CANCEL_ID_MISSING"}
        result = await self.execution.cancel_entry(
            run_id=run["run_id"],
            chain_id=chain["chain_id"],
            intent_id=intent["intent_id"],
            attempt_id=attempt["attempt_id"],
            broker_order_id=str(broker_order_id),
        )
        return _result_dict("cancel", result)

    async def _start_exit(
        self, run: dict[str, Any], snapshot: BrokerSnapshot, now: datetime
    ) -> dict[str, Any]:
        entry = self._active_entry_intent(run)
        if entry is None:
            self.store.activate_kill_switch(
                "open exposure has no durable entry intent",
                "worker",
                run_id=run["run_id"],
            )
            return {"status": "risk_off", "reason": "ENTRY_INTENT_MISSING"}
        intact, evidence = self._intact_entry_position(run, snapshot)
        if not intact:
            self.store.activate_kill_switch(
                "assignment or unmatched option legs detected",
                "worker",
                run_id=run["run_id"],
                evidence=evidence,
            )
            return {
                "status": "risk_off",
                "reason": "ASSIGNMENT_OR_UNMATCHED_LEGS",
                "manual_broker_intervention_required": True,
            }
        existing = self.store.list_order_intents(run["run_id"], purpose="exit")
        recovery_sequence = 0
        if existing:
            exit_chains = self.store.list_order_chains(
                run["run_id"], purpose="exit"
            )
            latest_chain = exit_chains[-1] if exit_chains else None
            latest_state = str(latest_chain.get("state")) if latest_chain else ""
            if latest_state == "FILLED":
                return {
                    "status": "exit_pending",
                    "awaiting_position_reconciliation": True,
                }
            if latest_state == "ERROR":
                self.store.activate_kill_switch(
                    "exit chain entered an unresolved error state",
                    "worker",
                    run_id=run["run_id"],
                    evidence={"latest_chain_state": latest_state},
                    activated_at=now,
                )
                return {
                    "status": "risk_off",
                    "reason": "EXIT_CHAIN_ERROR_UNRESOLVED",
                    "manual_broker_intervention_required": True,
                }
            if latest_state not in {"CANCELED", "REJECTED", "EXPIRED"}:
                return await self._reprice_active_order(
                    run, purpose="exit", now=now
                )
            if len(existing) >= MAX_EXIT_ORDER_CHAINS:
                self.store.activate_kill_switch(
                    "atomic exit exhausted deterministic retry limit",
                    "worker",
                    run_id=run["run_id"],
                    evidence={
                        "exit_chain_count": len(existing),
                        "latest_chain_state": latest_state,
                    },
                    activated_at=now,
                )
                return {
                    "status": "risk_off",
                    "reason": "EXIT_RETRY_LIMIT_EXHAUSTED",
                    "manual_broker_intervention_required": True,
                }
            recovery_sequence = len(existing)
        profile = self._run_strategy_profile(run)
        aggressive_intraday_exit = (
            profile is StrategyProfile.INTRADAY_CANARY
            and to_market_time(now).time()
            >= datetime.strptime("15:25", "%H:%M").time()
        )
        if aggressive_intraday_exit:
            limit_debit = _wing_width(entry["payload"])
        else:
            quote = await quote_atomic_exit(
                self.connection,
                entry["payload"],
                now=now,
                option_feed=self.settings.alpaca_option_feed,
            )
            limit_debit = quote.proposed_debit
        exit_intent = build_exit_from_entry_arguments(
            entry["payload"],
            limit_debit=limit_debit,
            environment=self.settings.environment,
            account_id=str(snapshot.account["id"]),
            event_date=datetime.fromisoformat(run["strategy_date"]).date(),
            strategy_version=(
                str(run["strategy_version"])
                if recovery_sequence == 0
                else (
                    f"{run['strategy_version']}-exit-recovery-"
                    f"{recovery_sequence}"
                )
            ),
        )
        self._persist_intent(run["run_id"], None, exit_intent)
        result = await self.execution.submit_exit(
            run_id=run["run_id"],
            intent_id=exit_intent.intent_id,
            chain_id=exit_intent.chain_id,
            attempt_id=exit_intent.attempt_id,
            arguments=exit_intent.arguments,
            now=now,
        )
        return _result_dict("exit", result)

    async def _reprice_active_order(
        self, run: dict[str, Any], *, purpose: str, now: datetime
    ) -> dict[str, Any]:
        if purpose == "entry":
            chain = self._active_entry_chain(run)
        else:
            chains = self.store.list_order_chains(run["run_id"], purpose=purpose)
            chain = chains[-1] if chains else None
        if chain is None:
            return {"status": f"monitoring_{purpose}", "repriced": False}
        intent = self.store.get_order_intent(str(chain["intent_id"]))
        if intent is None:
            return {"status": "risk_off", "reason": "ORDER_INTENT_MISSING"}
        attempt = self.store.latest_order_attempt(chain["chain_id"])

        if chain["state"] == "PLANNED":
            if purpose != "exit":
                return {"status": "monitoring_entry", "state": "PLANNED"}
            result = await self.execution.submit_exit(
                run_id=run["run_id"],
                intent_id=intent["intent_id"],
                chain_id=chain["chain_id"],
                attempt_id=_resume_attempt_id(chain["chain_id"]),
                arguments=intent["payload"],
                now=now,
            )
            return _result_dict("exit_resume_planned", result)

        if chain["state"] == "SUBMITTING":
            if attempt is None:
                if purpose == "exit":
                    result = await self.execution.submit_exit(
                        run_id=run["run_id"],
                        intent_id=intent["intent_id"],
                        chain_id=chain["chain_id"],
                        attempt_id=_resume_attempt_id(chain["chain_id"]),
                        arguments=intent["payload"],
                        now=now,
                    )
                    return _result_dict("exit_resume_pre_dispatch", result)
                self.store.activate_kill_switch(
                    f"{purpose} submission has no durable attempt",
                    "worker",
                    run_id=run["run_id"],
                    activated_at=now,
                )
                return {
                    "status": "risk_off",
                    "reason": "SUBMISSION_ATTEMPT_MISSING",
                    "manual_broker_intervention_required": True,
                }
            if purpose == "entry":
                result = await self.execution.resume_entry_submission(
                    run_id=run["run_id"],
                    intent_id=intent["intent_id"],
                    chain_id=chain["chain_id"],
                    attempt_id=attempt["attempt_id"],
                    arguments=intent["payload"],
                    now=now,
                )
            else:
                result = await self.execution.resume_exit_submission(
                    run_id=run["run_id"],
                    intent_id=intent["intent_id"],
                    chain_id=chain["chain_id"],
                    attempt_id=attempt["attempt_id"],
                    arguments=intent["payload"],
                    now=now,
                )
            return _result_dict(f"{purpose}_resume_submitting", result)

        if (
            chain["state"] == "REPLACEMENT_PENDING"
            and attempt is not None
            and not attempt.get("broker_order_id")
        ):
            result = await self.execution.resume_replacement(
                chain_id=chain["chain_id"],
                intent_id=intent["intent_id"],
                attempt_id=attempt["attempt_id"],
            )
            return self._handle_reprice_result(run, purpose=purpose, result=result)

        if chain["state"] not in {"PENDING", "UNKNOWN", "REPLACEMENT_PENDING"}:
            return {"status": f"monitoring_{purpose}", "state": chain["state"]}
        if not attempt or not attempt.get("broker_order_id"):
            self.store.activate_kill_switch(
                f"{purpose} broker identity remains unresolved",
                "worker",
                run_id=run["run_id"],
                evidence={"chain_state": chain["state"]},
                activated_at=now,
            )
            return {
                "status": "risk_off",
                "reason": "BROKER_ID_UNRESOLVED",
                "manual_broker_intervention_required": True,
            }
        created = datetime.fromisoformat(str(attempt["created_at"]))
        if now - created.astimezone(UTC) < timedelta(seconds=REPRICE_INTERVAL_SECONDS):
            return {"status": f"monitoring_{purpose}", "repriced": False}

        current_price = Decimal(str(attempt["request"].get("limit_price")))
        sequence = int(attempt["sequence"]) + 1
        client_order_id = _replacement_client_id(chain["chain_id"], sequence)
        if purpose == "entry":
            entry = intent
            candidate = next(
                (
                    item
                    for item in self.store.list_candidates(run["run_id"])
                    if item["candidate_id"] == entry.get("candidate_id")
                ),
                None,
            )
            if candidate is None:
                return {"status": "monitoring_entry", "reason": "CANDIDATE_MISSING"}
            current_credit = -current_price
            if self._run_strategy_profile(run) is StrategyProfile.INTRADAY_CANARY:
                floor = self.intraday_config.minimum_proposed_credit
            else:
                floor = Decimal(str(candidate["payload"]["natural_credit"]))
            next_credit = max(floor, current_credit - Decimal("0.05"))
            if next_credit >= current_credit:
                return {
                    "status": "monitoring_entry",
                    "repriced": False,
                    "at_policy_floor": True,
                }
            limit_price = "-" + _wire_decimal(next_credit)
            template = entry["payload"]
        else:
            entry = self._active_entry_intent(run)
            if entry is None:
                return {"status": "risk_off", "reason": "ENTRY_INTENT_MISSING"}
            wing_width = _wing_width(entry["payload"])
            profile = self._run_strategy_profile(run)
            aggressive_time = datetime.strptime(
                "15:25" if profile is StrategyProfile.INTRADAY_CANARY else "09:53",
                "%H:%M",
            ).time()
            aggressive_exit = to_market_time(now).time() >= aggressive_time
            if aggressive_exit:
                target = wing_width
            else:
                quote = await quote_atomic_exit(
                    self.connection,
                    entry["payload"],
                    now=now,
                    option_feed=self.settings.alpaca_option_feed,
                )
                target = min(wing_width, quote.natural_debit)
            next_debit = min(target, current_price + Decimal("0.05"))
            if aggressive_exit:
                next_debit = wing_width
            if next_debit <= current_price:
                return {
                    "status": "monitoring_exit",
                    "repriced": False,
                    "at_natural": True,
                }
            limit_price = _wire_decimal(next_debit)
            template = None

        result = await self.execution.replace_order(
            chain_id=chain["chain_id"],
            intent_id=intent["intent_id"],
            attempt_id=_replacement_attempt_id(chain["chain_id"], sequence),
            sequence=sequence,
            broker_order_id=attempt["broker_order_id"],
            client_order_id=client_order_id,
            limit_price=limit_price,
            entry_template=template,
        )
        return self._handle_reprice_result(run, purpose=purpose, result=result)

    def _handle_reprice_result(
        self,
        run: dict[str, Any],
        *,
        purpose: str,
        result: ExecutionResult,
    ) -> dict[str, Any]:
        if result.state == "FILLED":
            current = self.store.get_strategy_run(run["run_id"])
            if purpose == "entry":
                self.store.transition_strategy_run(
                    run["run_id"], "POSITION_OPEN", "REPLACEMENT_FILLED", {}
                )
            else:
                # Replacement fill status closes the order chain, not the
                # broker-position reconciliation.  A subsequent fresh cycle is
                # the only path to FLAT.
                if current is not None and current["state"] != "RISK_OFF":
                    self.store.transition_strategy_run(
                        run["run_id"],
                        "EXIT_PENDING",
                        "EXIT_REPLACEMENT_FILLED_AWAITING_POSITION_RECONCILIATION",
                        {"awaiting_position_reconciliation": True},
                    )
                payload = _result_dict(f"{purpose}_reprice", result)
                payload["status"] = (
                    "risk_off"
                    if current is not None and current["state"] == "RISK_OFF"
                    else "exit_pending"
                )
                payload["awaiting_position_reconciliation"] = True
                return payload
        elif result.state in {"CANCELED", "REJECTED", "EXPIRED"}:
            if purpose == "entry":
                self.store.transition_strategy_run(
                    run["run_id"], "CANCEL_PENDING", "REPLACEMENT_TERMINAL", {}
                )
                self.store.transition_strategy_run(
                    run["run_id"], "NO_TRADE", "REPLACEMENT_ZERO_FILL", {}
                )
            else:
                self.store.transition_strategy_run(
                    run["run_id"], "RISK_OFF", "EXIT_REPLACEMENT_TERMINAL", {}
                )
        elif result.state == "UNKNOWN":
            self.store.activate_kill_switch(
                f"{purpose} replacement outcome is ambiguous",
                "worker",
                run_id=run["run_id"],
                evidence={"broker_status": result.broker_status},
            )
            payload = _result_dict(f"{purpose}_reprice", result)
            payload["status"] = "risk_off"
            payload["manual_broker_intervention_required"] = True
            return payload
        return _result_dict(f"{purpose}_reprice", result)

    def _create_run(
        self, strategy_date: Any, events: tuple[EventDefinition, ...]
    ) -> dict[str, Any]:
        run_id = (
            f"tt-run-{self.settings.environment[:8]}-{strategy_date.isoformat()}-"
            f"{self.config_hash[:16]}"
        )
        return self.store.create_strategy_run(
            run_id,
            environment=self.settings.environment,
            strategy_date=strategy_date.isoformat(),
            strategy_version=self.events.strategy_version,
            config_hash=self.config_hash,
            context={
                "symbols": [event.symbol for event in events],
                "strategy_profile": StrategyProfile.EARNINGS.value,
                "strategy_name": "ThetaTrap earnings iron condor",
                "paper_only": True,
                "data_feed": "basic_indicative",
                "market_data_profile": self.settings.market_data_status(),
            },
        )

    def _create_intraday_run(self) -> dict[str, Any]:
        strategy_date = self.intraday_config.trade_date
        run_id = (
            f"tt-run-{self.settings.environment[:8]}-{strategy_date.isoformat()}-"
            f"{self.config_hash[:16]}"
        )
        return self.store.create_strategy_run(
            run_id,
            environment=self.settings.environment,
            strategy_date=strategy_date.isoformat(),
            strategy_version=INTRADAY_STRATEGY_VERSION,
            config_hash=self.config_hash,
            context={
                "symbols": list(self.intraday_config.allowed_symbols),
                "strategy_profile": StrategyProfile.INTRADAY_CANARY.value,
                "strategy_profile_id": INTRADAY_PROFILE_ID,
                "strategy_name": INTRADAY_STRATEGY_NAME,
                "profile_kind": "final_day_intraday_contingency",
                "activation_reason": (
                    "The earnings profile remained flat after Basic indicative "
                    "liquidity gates rejected every Sep 1-2 candidate."
                ),
                "trade_date": strategy_date.isoformat(),
                "expiration": self.intraday_config.expiration.isoformat(),
                "entry_window_et": {
                    "start": SEP3_INTRADAY_CANARY_SESSION.entry_start.isoformat(
                        timespec="minutes"
                    ),
                    "stop_new_orders": (
                        SEP3_INTRADAY_CANARY_SESSION.stop_new_orders.isoformat(
                            timespec="minutes"
                        )
                    ),
                    "cancel_all_unfilled": (
                        SEP3_INTRADAY_CANARY_SESSION.cancel_all_unfilled.isoformat(
                            timespec="minutes"
                        )
                    ),
                },
                "exit_window_et": {
                    "start": SEP3_INTRADAY_CANARY_SESSION.exit_start.isoformat(
                        timespec="minutes"
                    ),
                    "aggressive_limit": "15:25",
                    "broker_flat_target": "15:45",
                },
                "structure": {
                    "name": "exact_1_dollar_symmetric_iron_condor",
                    "quantity": self.intraday_config.quantity,
                    "wing_width": str(self.intraday_config.wing_width),
                    "minimum_credit": str(
                        self.intraday_config.minimum_proposed_credit
                    ),
                    "maximum_loss_dollars": str(
                        self.intraday_config.maximum_loss_dollars
                    ),
                },
                "paper_only": True,
                "data_feed": "basic_indicative",
                "market_data_profile": self.settings.market_data_status(),
                "profitability_claim": "none",
            },
        )

    def _run_strategy_profile(self, run: dict[str, Any]) -> StrategyProfile:
        context = run.get("context")
        value = context.get("strategy_profile") if isinstance(context, dict) else None
        try:
            return StrategyProfile(str(value or StrategyProfile.EARNINGS.value))
        except ValueError:
            return StrategyProfile.EARNINGS

    def _active_entry_intent(
        self, run: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Resolve the sole submitted entry; never infer it from list order."""

        authorization = self.store.get_entry_authorization(
            environment=self.settings.environment,
            account_id=self.settings.expected_account_id,
            strategy_date=str(run["strategy_date"]),
        )
        if (
            authorization is not None
            and authorization.get("state") == "CONSUMED"
            and authorization.get("consumed_run_id") == run["run_id"]
            and authorization.get("consumed_intent_id")
        ):
            intent = self.store.get_order_intent(
                str(authorization["consumed_intent_id"])
            )
            if (
                intent is not None
                and intent["run_id"] == run["run_id"]
                and intent["purpose"] == "entry"
            ):
                return intent

        # Tests and recovery from older databases may predate authorization
        # linkage.  A durable submitted chain is the only safe fallback.
        chains = self.store.list_order_chains(run["run_id"], purpose="entry")
        for chain in reversed(chains):
            if self.store.latest_order_attempt(chain["chain_id"]) is None:
                continue
            intent = self.store.get_order_intent(str(chain["intent_id"]))
            if intent is not None:
                return intent

        entries = self.store.list_order_intents(run["run_id"], purpose="entry")
        return entries[0] if len(entries) == 1 else None

    def _active_entry_chain(self, run: dict[str, Any]) -> dict[str, Any] | None:
        intent = self._active_entry_intent(run)
        if intent is None:
            return None
        return next(
            (
                chain
                for chain in self.store.list_order_chains(
                    run["run_id"], purpose="entry"
                )
                if chain["intent_id"] == intent["intent_id"]
            ),
            None,
        )

    def _return_run_to_screening(
        self,
        run_id: str,
        reason_code: str,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.store.get_strategy_run(run_id)
        if current is None:
            raise PolicyError("strategy run disappeared during candidate review")
        if current["state"] == "SCREENING":
            return current
        if current["state"] not in {"AI_REVIEW", "POLICY_CHECK"}:
            raise PolicyError(
                "candidate review cannot resume screening from "
                + str(current["state"])
            )
        return self.store.transition_strategy_run(
            run_id,
            "SCREENING",
            reason_code,
            evidence or {},
        )

    def _cancel_unfilled_time(self, run: dict[str, Any]) -> Any:
        if self._run_strategy_profile(run) is StrategyProfile.INTRADAY_CANARY:
            return SEP3_INTRADAY_CANARY_SESSION.cancel_all_unfilled
        return self.events.entry_window.cancel_all_unfilled

    def _intraday_strategy_context(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "strategy_profile": StrategyProfile.INTRADAY_CANARY.value,
            "strategy_profile_id": INTRADAY_PROFILE_ID,
            "strategy_name": INTRADAY_STRATEGY_NAME,
            "event_dependency": False,
            "event_date": self.intraday_config.trade_date.isoformat(),
            "release_timing": "not_applicable_intraday",
            "status": "date_bounded_canary",
            "expiration": self.intraday_config.expiration.isoformat(),
            "exit_required_same_day": True,
            "paper_only": True,
        }

    def _persist_collection(self, run_id: str, collection: MarketCollection) -> None:
        self.store.record_collection_snapshot(
            collection.collection_id,
            run_id=run_id,
            symbol=collection.symbol,
            collection_type="strategy_market_bundle",
            observed_at=collection.collected_at,
            payload=serialize_for_storage(collection),
        )

    def _persist_evaluation(
        self,
        run_id: str,
        collection: MarketCollection,
        evaluation: Any,
        *,
        rank: int | None,
    ) -> str:
        candidate_id = _candidate_id(run_id, collection)
        self.store.record_candidate(
            candidate_id,
            run_id=run_id,
            snapshot_id=collection.collection_id,
            symbol=evaluation.symbol,
            candidate_rank=rank,
            eligible=evaluation.eligible,
            payload=serialize_evaluation(evaluation),
        )
        for failure in evaluation.failures:
            self.store.record_gate_result(
                candidate_id,
                collection.source_digest,
                failure.code.value,
                passed=False,
                reason_code=failure.code.value,
                detail={"detail": failure.detail},
                evaluated_at=collection.collected_at,
            )
        return candidate_id

    def _persist_candidate(
        self, run_id: str, collection: MarketCollection, candidate: Any, *, rank: int
    ) -> str:
        candidate_id = _candidate_id(run_id, collection)
        self.store.record_candidate(
            candidate_id,
            run_id=run_id,
            snapshot_id=collection.collection_id,
            symbol=candidate.symbol,
            candidate_rank=rank,
            eligible=True,
            payload=serialize_candidate(candidate),
        )
        self.store.record_gate_result(
            candidate_id,
            collection.source_digest,
            "ALL_DETERMINISTIC_GATES",
            passed=True,
            detail={"risk_budget": str(candidate.risk_budget)},
            evaluated_at=collection.collected_at,
        )
        return candidate_id

    def _persist_intent(
        self, run_id: str, candidate_id: str | None, intent: OrderIntent
    ) -> None:
        self.store.record_order_intent(
            intent.intent_id,
            run_id=run_id,
            candidate_id=candidate_id,
            purpose=intent.purpose.value,
            client_order_id=intent.client_order_id,
            payload=intent.arguments,
        )
        self.store.create_order_chain(
            intent.chain_id,
            run_id=run_id,
            intent_id=intent.intent_id,
            purpose=intent.purpose.value,
        )

    def _persist_agent_tools(
        self, agent_run_id: str, tools: RuntimeAgentTools, called_at: datetime
    ) -> None:
        for call in tools.calls:
            self.store.record_agent_tool_call(
                agent_run_id,
                call.sequence,
                principal="qwen",
                tool_name=call.name,
                arguments=call.arguments,
                result=call.result,
                status=call.status,
                duration_ms=call.duration_ms,
                is_official_mcp=call.is_official_mcp,
                called_at=called_at,
            )


def _candidate_id(run_id: str, collection: MarketCollection) -> str:
    digest = hashlib.sha256(
        f"{run_id}|{collection.symbol}|{collection.source_digest}".encode("utf-8")
    ).hexdigest()[:32]
    return f"tt-candidate-{digest}"


def _replacement_client_id(chain_id: str, sequence: int) -> str:
    digest = hashlib.sha256(f"{chain_id}|{sequence}".encode("utf-8")).hexdigest()[:24]
    return f"tt-replace-{sequence}-{digest}"


def _replacement_attempt_id(chain_id: str, sequence: int) -> str:
    digest = hashlib.sha256(
        f"attempt|{chain_id}|{sequence}".encode("utf-8")
    ).hexdigest()[:24]
    return f"tt-attempt-r-{sequence}-{digest}"


def _resume_attempt_id(chain_id: str) -> str:
    digest = hashlib.sha256(f"resume|{chain_id}".encode("utf-8")).hexdigest()[:24]
    return f"tt-attempt-resume-{digest}"


def _wire_decimal(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _wing_width(entry_arguments: dict[str, Any]) -> Decimal:
    symbols = [leg["symbol"] for leg in entry_arguments["legs"]]
    strikes = [Decimal(symbol[-8:]) / Decimal("1000") for symbol in symbols]
    return strikes[1] - strikes[0]


def _result_dict(action: str, result: ExecutionResult) -> dict[str, Any]:
    return {
        "status": result.state.lower(),
        "action": action,
        "broker_status": result.broker_status,
        "broker_order_id": result.broker_order_id,
        "reconciled_after_error": result.reconciled_after_error,
    }


def _serialize_intraday_config(config: IntradayStrategyConfig) -> dict[str, Any]:
    raw = {field.name: getattr(config, field.name) for field in fields(config)}
    raw["accepted_open_interest_dates"] = tuple(
        sorted(config.accepted_open_interest_dates)
    )
    serialized = serialize_for_storage(raw)
    if not isinstance(serialized, dict):  # pragma: no cover - mapping invariant
        raise TypeError("serialized intraday configuration must be an object")
    return serialized
