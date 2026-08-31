"""Autonomous ThetaTrap scheduler and persisted strategy state machine."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

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
    ScheduleAction,
    action_for_time,
    to_market_time,
    verified_events_for_day,
)
from thetatrap.settings import RuntimeSettings, account_suffix
from thetatrap.storage import Store
from thetatrap.strategy import StrategyConfig, evaluate_symbol, rank_candidates


LOGGER = logging.getLogger(__name__)
LEASE_NAME = "thetatrap-worker-cycle"
LEASE_TTL_SECONDS = 180
REPRICE_INTERVAL_SECONDS = 30
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
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.connection = connection
        self.events = events or load_events()
        self.strategy_config = strategy_config or StrategyConfig()
        self.execution = ExecutionService(settings, store, connection)
        self.owner_id = str(uuid.uuid4())
        self.agent_factory = agent_factory or (
            lambda configured, tools: QwenAgent(configured, tools)
        )
        self.config_hash = payload_hash(
            {
                "events": self.events.model_dump(mode="json"),
                "strategy": serialize_for_storage(self.strategy_config),
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
        action = action_for_time(
            now=now,
            config=self.events,
            open_trade_date=open_trade_date,
            has_working_entry=has_working_entry,
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
            result = await self._scan_entry(snapshot, now)
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
        if state in {"SUBMITTING", "ORDER_PENDING", "CANCEL_PENDING", "RISK_OFF"}:
            chains = self.store.list_order_chains(run_id, purpose="entry")
            if chains and chains[-1]["state"] not in {
                "CANCELED",
                "FILLED",
                "REJECTED",
                "EXPIRED",
            }:
                attempt = self.store.latest_order_attempt(chains[-1]["chain_id"])
                if attempt is not None:
                    await self.execution.reconcile_entry_order(
                        run_id=run_id,
                        chain_id=chains[-1]["chain_id"],
                        attempt_id=attempt["attempt_id"],
                        client_order_id=attempt["client_order_id"],
                    )
                    run = self.store.get_strategy_run(run_id) or run
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
            and state
            in {"POSITION_OPEN", "EXIT_SUBMITTING", "EXIT_PENDING", "RISK_OFF"}
        ):
            run = self.store.transition_strategy_run(
                run_id, "FLAT", "BROKER_FLAT_RECONCILED", {}
            )
        return run

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
            if action is ScheduleAction.ENTRY_SCAN:
                return await self._reprice_active_order(run, purpose="entry", now=now)
            return {"status": "monitoring_entry", "state": state}
        if state == "POSITION_OPEN":
            if action is ScheduleAction.EXIT:
                return await self._start_exit(run, snapshot, now)
            return {"status": "position_open", "state": state}
        if state in {"EXIT_SUBMITTING", "EXIT_PENDING"}:
            if action is ScheduleAction.EXIT:
                return await self._reprice_active_order(run, purpose="exit", now=now)
            return {"status": "monitoring_exit", "state": state}
        if state == "SCREENING" and action is ScheduleAction.ENTRY_SCAN:
            return await self._scan_entry(snapshot, now, existing_run=run)
        if (
            state == "SCREENING"
            and to_market_time(now).time()
            >= self.events.entry_window.cancel_all_unfilled
        ):
            self.store.transition_strategy_run(
                run["run_id"], "NO_TRADE", "ENTRY_WINDOW_EXPIRED", {}
            )
            return {"status": "no_trade", "reason": "ENTRY_WINDOW_EXPIRED"}
        return {"status": "monitoring", "state": state, "action": action.value}

    def _working_chain_attempt(
        self, run_id: str, *, purpose: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        chains = self.store.list_order_chains(run_id, purpose=purpose)
        if not chains or chains[-1]["state"] not in WORKING_CHAIN_STATES:
            return None, None
        chain = chains[-1]
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
        entries = self.store.list_order_intents(run["run_id"], purpose="entry")
        if not entries:
            return False, {"reason": "ENTRY_INTENT_MISSING"}
        payload = entries[0]["payload"]
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
            return {
                "status": "screening",
                "eligible_candidates": 0,
                "rejected_candidates": rejected,
                "collection_errors": collection_errors,
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

    async def _review_candidate(
        self,
        run: dict[str, Any],
        snapshot: BrokerSnapshot,
        candidate: Any,
        candidate_id: str,
        event: EventDefinition,
        now: datetime,
    ) -> dict[str, Any] | None:
        intent = build_entry_order_intent(
            candidate,
            environment=self.settings.environment,
            account_id=str(snapshot.account["id"]),
            event_date=event.event_date,
            strategy_version=self.events.strategy_version,
        )
        self._persist_intent(run["run_id"], candidate_id, intent)
        context = AgentContext(
            symbol=candidate.symbol,
            event=event.model_dump(mode="json"),
            candidate=serialize_candidate(candidate),
            run_summary={
                "run_id": run["run_id"],
                "state": "AI_REVIEW",
                "environment": self.settings.environment,
                "paper_only": True,
                "execution_enabled": self.settings.execution_enabled,
            },
            order_intent_id=intent.intent_id,
            order_arguments=intent.arguments,
        )
        tools = RuntimeAgentTools(
            self.connection,
            context,
            verified_events=[
                item.model_dump(mode="json")
                for item in self.events.events
                if item.status == "verified"
            ],
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
                fresh = await self._fresh_revalidate(
                    run, snapshot, candidate, event, intent, now
                )
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
                result = await self.execution.submit_entry(
                    run_id=run["run_id"],
                    intent_id=intent.intent_id,
                    chain_id=intent.chain_id,
                    attempt_id=intent.attempt_id,
                    arguments=intent.arguments,
                )
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

    async def _fresh_revalidate(
        self,
        run: dict[str, Any],
        snapshot: BrokerSnapshot,
        original: Any,
        event: EventDefinition,
        intent: OrderIntent,
        now: datetime,
    ) -> bool:
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

    async def _cancel_active_entry(self, run: dict[str, Any]) -> dict[str, Any]:
        chains = self.store.list_order_chains(run["run_id"], purpose="entry")
        if not chains:
            self.store.transition_strategy_run(
                run["run_id"], "NO_TRADE", "NO_ENTRY_ORDER_TO_CANCEL", {}
            )
            return {"status": "no_trade", "reason": "NO_ENTRY_ORDER_TO_CANCEL"}
        chain = chains[-1]
        attempt = self.store.latest_order_attempt(chain["chain_id"])
        intents = self.store.list_order_intents(run["run_id"], purpose="entry")
        if not attempt or not attempt.get("broker_order_id") or not intents:
            self.store.activate_kill_switch(
                "working entry could not be identified for cancellation",
                "worker",
                run_id=run["run_id"],
            )
            return {"status": "risk_off", "reason": "ENTRY_CANCEL_ID_MISSING"}
        result = await self.execution.cancel_entry(
            run_id=run["run_id"],
            chain_id=chain["chain_id"],
            intent_id=intents[0]["intent_id"],
            attempt_id=attempt["attempt_id"],
            broker_order_id=attempt["broker_order_id"],
        )
        return _result_dict("cancel", result)

    async def _start_exit(
        self, run: dict[str, Any], snapshot: BrokerSnapshot, now: datetime
    ) -> dict[str, Any]:
        entries = self.store.list_order_intents(run["run_id"], purpose="entry")
        if not entries:
            self.store.activate_kill_switch(
                "open exposure has no durable entry intent",
                "worker",
                run_id=run["run_id"],
            )
            return {"status": "risk_off", "reason": "ENTRY_INTENT_MISSING"}
        entry = entries[0]
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
        quote = await quote_atomic_exit(self.connection, entry["payload"], now=now)
        exit_intent = build_exit_from_entry_arguments(
            entry["payload"],
            limit_debit=quote.proposed_debit,
            environment=self.settings.environment,
            account_id=str(snapshot.account["id"]),
            event_date=datetime.fromisoformat(run["strategy_date"]).date(),
            strategy_version=self.events.strategy_version,
        )
        existing = self.store.list_order_intents(run["run_id"], purpose="exit")
        if existing:
            return await self._reprice_active_order(run, purpose="exit", now=now)
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
        chains = self.store.list_order_chains(run["run_id"], purpose=purpose)
        intents = self.store.list_order_intents(run["run_id"], purpose=purpose)
        if not chains or not intents:
            return {"status": f"monitoring_{purpose}", "repriced": False}
        chain = chains[-1]
        if chain["state"] not in {"PENDING", "UNKNOWN", "REPLACEMENT_PENDING"}:
            return {"status": f"monitoring_{purpose}", "state": chain["state"]}
        attempt = self.store.latest_order_attempt(chain["chain_id"])
        if not attempt or not attempt.get("broker_order_id"):
            return {"status": f"monitoring_{purpose}", "reason": "BROKER_ID_PENDING"}
        created = datetime.fromisoformat(str(attempt["created_at"]))
        if now - created.astimezone(UTC) < timedelta(seconds=REPRICE_INTERVAL_SECONDS):
            return {"status": f"monitoring_{purpose}", "repriced": False}

        current_price = Decimal(str(attempt["request"].get("limit_price")))
        sequence = int(attempt["sequence"]) + 1
        client_order_id = _replacement_client_id(chain["chain_id"], sequence)
        if purpose == "entry":
            entry = intents[0]
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
            natural = Decimal(str(candidate["payload"]["natural_credit"]))
            current_credit = -current_price
            next_credit = max(natural, current_credit - Decimal("0.05"))
            if next_credit >= current_credit:
                return {
                    "status": "monitoring_entry",
                    "repriced": False,
                    "at_natural": True,
                }
            limit_price = "-" + _wire_decimal(next_credit)
            template = entry["payload"]
        else:
            entry_intents = self.store.list_order_intents(
                run["run_id"], purpose="entry"
            )
            if not entry_intents:
                return {"status": "risk_off", "reason": "ENTRY_INTENT_MISSING"}
            quote = await quote_atomic_exit(
                self.connection, entry_intents[0]["payload"], now=now
            )
            wing_width = _wing_width(entry_intents[0]["payload"])
            target = (
                wing_width
                if to_market_time(now).time()
                >= datetime.strptime("09:53", "%H:%M").time()
                else min(wing_width, quote.natural_debit)
            )
            next_debit = min(target, current_price + Decimal("0.05"))
            if to_market_time(now).time() >= datetime.strptime("09:53", "%H:%M").time():
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
            intent_id=intents[0]["intent_id"],
            attempt_id=_replacement_attempt_id(chain["chain_id"], sequence),
            sequence=sequence,
            broker_order_id=attempt["broker_order_id"],
            client_order_id=client_order_id,
            limit_price=limit_price,
            entry_template=template,
        )
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
                "paper_only": True,
                "data_feed": "basic_indicative",
            },
        )

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
