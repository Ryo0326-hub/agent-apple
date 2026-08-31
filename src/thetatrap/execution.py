"""Idempotent MCP-only order execution and broker reconciliation.

This module never constructs strategy economics and never calls Alpaca REST or
an SDK.  It accepts an immutable, already-persisted order intent, performs a
fresh broker admission check, persists SUBMITTING before the side effect, and
uses the deterministic client order ID to reconcile every ambiguous response.
"""

from __future__ import annotations

import uuid
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from thetatrap.errors import ExecutionError, PolicyError
from thetatrap.mcp.client import MCPConnection, extract_account, unwrap_data
from thetatrap.policy import (
    MutationPurpose,
    make_entry_permit,
    make_system_permit,
    validate_mleg_arguments,
)
from thetatrap.settings import RuntimeSettings
from thetatrap.storage import StorageInvariantError, Store
from thetatrap.strategy import round_debit_up


KILL_EQUITY = Decimal("99000")
INITIAL_COMPETITION_EQUITY = Decimal("100000")
OPEN_ORDER_STATUSES = frozenset(
    {
        "accepted",
        "accepted_for_bidding",
        "calculated",
        "held",
        "new",
        "partially_filled",
        "pending_cancel",
        "pending_new",
        "pending_replace",
        "stopped",
    }
)
TERMINAL_ZERO_FILL_STATUSES = frozenset(
    {"canceled", "cancelled", "expired", "rejected", "suspended"}
)


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    observed_at: datetime
    account: dict[str, Any]
    account_config: dict[str, Any]
    clock: dict[str, Any]
    open_orders: tuple[dict[str, Any], ...]
    positions: tuple[dict[str, Any], ...]
    equity: Decimal
    buying_power: Decimal
    options_level: int
    market_is_open: bool

    @property
    def is_flat(self) -> bool:
        return not self.positions


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    state: str
    broker_status: str | None
    broker_order_id: str | None
    reconciled_after_error: bool
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExitQuote:
    observed_at: datetime
    midpoint_debit: Decimal
    natural_debit: Decimal
    proposed_debit: Decimal
    tick_size: Decimal
    oldest_quote_at: datetime


@dataclass(frozen=True, slots=True)
class ActivityReconciliation:
    observed: int
    fills_recorded: int
    assignment_or_exercise_detected: bool
    activity_types: tuple[str, ...]


class ExecutionService:
    def __init__(
        self,
        settings: RuntimeSettings,
        store: Store,
        connection: MCPConnection,
    ) -> None:
        self.settings = settings
        self.store = store
        self.connection = connection

    async def read_broker_snapshot(
        self, *, run_id: str | None = None, now: datetime | None = None
    ) -> BrokerSnapshot:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        account = extract_account(
            await self.connection.call_system_read("get_account_info", {})
        )
        # Identity is checked before any other account-scoped broker read.
        self.store.bind_identity(
            self.settings.environment, self.settings.expected_account_id, account["id"]
        )
        account_config = _object_data(
            await self.connection.call_system_read("get_account_config", {})
        )
        clock = _object_data(await self.connection.call_system_read("get_clock", {}))
        orders = _result_list(
            await self.connection.call_system_read(
                "get_orders",
                {
                    "status": "open",
                    "nested": True,
                    "limit": 100,
                },
            )
        )
        positions = _result_list(
            await self.connection.call_system_read("get_all_positions", {})
        )
        equity = _decimal(account.get("equity"), "account equity")
        buying_power = _decimal(account.get("buying_power"), "buying power")
        options_level = _options_level(account, account_config)
        market_is_open = clock.get("is_open") is True

        self.store.record_account_snapshot(account)
        self.store.record_equity_observation(
            str(uuid.uuid4()),
            run_id=run_id,
            observed_at=observed_at,
            equity=str(equity),
            buying_power=str(buying_power),
            cash=_optional_text(account.get("cash")),
            portfolio_value=_optional_text(account.get("portfolio_value")),
            payload={"status": account.get("status")},
        )
        self.store.record_position_observation(
            str(uuid.uuid4()),
            run_id=run_id,
            observed_at=observed_at,
            positions=positions,
            is_flat=not positions,
        )
        return BrokerSnapshot(
            observed_at=observed_at,
            account=account,
            account_config=account_config,
            clock=clock,
            open_orders=tuple(orders),
            positions=tuple(positions),
            equity=equity,
            buying_power=buying_power,
            options_level=options_level,
            market_is_open=market_is_open,
        )

    def entry_admission(
        self, snapshot: BrokerSnapshot, *, strategy_date: str | None = None
    ) -> AdmissionResult:
        reasons: list[str] = []
        if not self.settings.execution_enabled or self.settings.read_only:
            reasons.append("EXECUTION_DISARMED")
        if str(snapshot.account.get("status") or "").upper() != "ACTIVE":
            reasons.append("ACCOUNT_NOT_ACTIVE")
        if any(
            _broker_flag(snapshot.account.get(name))
            for name in ("account_blocked", "trading_blocked", "trade_suspended")
        ):
            reasons.append("ACCOUNT_TRADING_BLOCKED")
        if snapshot.options_level < 3:
            reasons.append("OPTIONS_LEVEL_BELOW_3")
        if not snapshot.market_is_open:
            reasons.append("MARKET_CLOSED")
        if snapshot.open_orders:
            reasons.append("OPEN_ORDER_EXISTS")
        if snapshot.positions:
            reasons.append("POSITION_EXISTS")
        if snapshot.equity <= KILL_EQUITY:
            reasons.append("EQUITY_KILL_THRESHOLD")
        if self.store.get_kill_switch()["kill_switch_enabled"]:
            reasons.append("KILL_SWITCH_ACTIVE")
        if (
            self.settings.environment == "competition"
            and self.store.get_metadata("initial_admission") is None
            and snapshot.equity != INITIAL_COMPETITION_EQUITY
        ):
            reasons.append("INITIAL_EQUITY_NOT_100000")
        if strategy_date is not None:
            authorization = self.store.get_entry_authorization(
                environment=self.settings.environment,
                account_id=str(snapshot.account["id"]),
                strategy_date=strategy_date,
            )
            authorization_reason = _entry_authorization_blocker(
                authorization, snapshot.observed_at
            )
            if authorization_reason is not None:
                reasons.append(authorization_reason)
        return AdmissionResult(not reasons, tuple(reasons))

    async def reconcile_account_activities(
        self,
        *,
        run_id: str,
        after: datetime,
        now: datetime | None = None,
    ) -> ActivityReconciliation:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        wrapper = await self.connection.call_system_read(
            "get_account_activities",
            {
                "activity_types": ["FILL", "OPASN", "OPEXC", "OPEXP", "OPTRD", "OPCA"],
                "after": after.astimezone(UTC).isoformat(),
                "direction": "asc",
                "page_size": 100,
            },
        )
        activities = _result_list(wrapper)
        recorded_fills = 0
        assignment = False
        types: set[str] = set()
        for activity in activities:
            activity_type = str(
                activity.get("activity_type") or activity.get("type") or "UNKNOWN"
            ).upper()
            types.add(activity_type)
            if activity_type in {"OPASN", "OPEXC", "OPCA"}:
                assignment = True
            occurred = _activity_time(activity, observed_at)
            activity_id = str(activity.get("id") or activity.get("activity_id") or "")
            if not activity_id:
                activity_id = "tt-activity-" + hashlib.sha256(
                    json.dumps(activity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
                ).hexdigest()[:32]
            self.store.record_broker_activity(
                activity_id,
                run_id=run_id,
                activity_type=activity_type,
                occurred_at=occurred,
                payload=activity,
            )
            if activity_type != "FILL":
                continue
            broker_order_id = str(activity.get("order_id") or "")
            attempt = (
                self.store.find_order_attempt_by_broker_id(broker_order_id)
                if broker_order_id
                else None
            )
            symbol = activity.get("symbol")
            side = activity.get("side")
            quantity = activity.get("qty", activity.get("quantity"))
            price = activity.get("price")
            if (
                attempt is None
                or not symbol
                or side not in {"buy", "sell"}
                or quantity is None
                or price is None
            ):
                continue
            self.store.record_fill(
                activity_id,
                chain_id=attempt["chain_id"],
                attempt_id=attempt["attempt_id"],
                broker_order_id=broker_order_id,
                symbol=str(symbol),
                side=str(side),
                quantity=str(quantity),
                price=str(price),
                filled_at=occurred,
                payload=activity,
            )
            recorded_fills += 1
        return ActivityReconciliation(
            observed=len(activities),
            fills_recorded=recorded_fills,
            assignment_or_exercise_detected=assignment,
            activity_types=tuple(sorted(types)),
        )

    async def submit_entry(
        self,
        *,
        run_id: str,
        intent_id: str,
        chain_id: str,
        attempt_id: str,
        arguments: dict[str, Any],
        now: datetime | None = None,
    ) -> ExecutionResult:
        validate_mleg_arguments(arguments, action="entry")
        if not self.store.order_intent_matches(intent_id, arguments):
            raise PolicyError("entry arguments do not match the durable order intent")
        run = self.store.get_strategy_run(run_id)
        if run is None:
            raise PolicyError("entry strategy run does not exist")
        strategy_date = str(run["strategy_date"])
        snapshot = await self.read_broker_snapshot(run_id=run_id, now=now)
        admission = self.entry_admission(snapshot, strategy_date=strategy_date)
        if not admission.allowed:
            raise PolicyError("entry admission failed: " + ", ".join(admission.reasons))
        authorization = self.store.get_entry_authorization(
            environment=self.settings.environment,
            account_id=str(snapshot.account["id"]),
            strategy_date=strategy_date,
        )
        if authorization is None:  # guarded by entry_admission; retained fail-closed
            raise PolicyError("entry authorization is missing")
        if self.store.get_metadata("initial_admission") is None:
            self.store.record_initial_admission(
                environment=self.settings.environment, equity=str(snapshot.equity)
            )
        permit = make_entry_permit(
            intent_id=intent_id, arguments=arguments, now=snapshot.observed_at
        )
        try:
            self.store.begin_authorized_entry_submission(
                str(authorization["authorization_id"]),
                environment=self.settings.environment,
                account_id=str(snapshot.account["id"]),
                strategy_date=strategy_date,
                run_id=run_id,
                intent_id=intent_id,
                chain_id=chain_id,
                attempt_id=attempt_id,
                client_order_id=str(arguments["client_order_id"]),
                request=arguments,
                observed_at=snapshot.observed_at,
            )
        except StorageInvariantError as exc:
            raise PolicyError(f"entry authorization failed: {exc}") from exc
        try:
            wrapper = await self.connection.call_mutation(
                "place_option_order",
                arguments,
                permit=permit,
                principal="agent",
            )
            order = _order_data(wrapper)
            return self._apply_entry_order(
                run_id, chain_id, attempt_id, order, reconciled=False
            )
        except Exception as exc:
            return await self._reconcile_ambiguous_entry(
                run_id=run_id,
                chain_id=chain_id,
                attempt_id=attempt_id,
                client_order_id=str(arguments["client_order_id"]),
                original_error=exc,
            )

    async def replace_order(
        self,
        *,
        chain_id: str,
        intent_id: str,
        attempt_id: str,
        sequence: int,
        broker_order_id: str,
        client_order_id: str,
        limit_price: str,
        entry_template: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        if sequence < 1:
            raise PolicyError("replacement sequence must be positive")
        if entry_template is not None:
            validate_mleg_arguments(
                {
                    **entry_template,
                    "client_order_id": client_order_id,
                    "limit_price": limit_price,
                },
                action="entry",
            )
        arguments = {
            "order_id": broker_order_id,
            "limit_price": limit_price,
            "client_order_id": client_order_id,
        }
        self.store.transition_order_chain(
            chain_id, "REPLACEMENT_PENDING", detail={"sequence": sequence}
        )
        self.store.record_order_attempt(
            attempt_id,
            chain_id=chain_id,
            sequence=sequence,
            client_order_id=client_order_id,
            request=arguments,
        )
        permit = make_system_permit(
            tool_name="replace_order_by_id",
            purpose=MutationPurpose.REPRICE,
            intent_id=intent_id,
            arguments=arguments,
        )
        try:
            wrapper = await self.connection.call_mutation(
                "replace_order_by_id", arguments, permit=permit, principal="system"
            )
            order = _order_data(wrapper)
        except Exception as exc:
            order = await self._lookup_by_client_id(client_order_id)
            if order is None:
                self.store.transition_order_chain(
                    chain_id,
                    "UNKNOWN",
                    attempt_id=attempt_id,
                    detail={"error_type": type(exc).__name__},
                )
                return ExecutionResult(
                    "UNKNOWN", None, None, True, {"error_type": type(exc).__name__}
                )
        return self._apply_chain_order(chain_id, attempt_id, order, reconciled=False)

    async def cancel_entry(
        self,
        *,
        run_id: str,
        chain_id: str,
        intent_id: str,
        attempt_id: str,
        broker_order_id: str,
    ) -> ExecutionResult:
        arguments = {"order_id": broker_order_id}
        current_run = self.store.get_strategy_run(run_id)
        risk_off = bool(current_run and current_run["state"] == "RISK_OFF")
        if not risk_off:
            self.store.transition_strategy_run(
                run_id, "CANCEL_PENDING", "ENTRY_CUTOFF", {"order_id": broker_order_id}
            )
        self.store.transition_order_chain(
            chain_id, "CANCEL_PENDING", attempt_id=attempt_id
        )
        permit = make_system_permit(
            tool_name="cancel_order_by_id",
            purpose=MutationPurpose.CANCEL,
            intent_id=intent_id,
            arguments=arguments,
        )
        try:
            await self.connection.call_mutation(
                "cancel_order_by_id", arguments, permit=permit, principal="system"
            )
        except Exception:
            # Cancellation is also ambiguous; broker truth below decides.
            pass
        wrapper = await self.connection.call_system_read(
            "get_order_by_id", {"order_id": broker_order_id, "nested": True}
        )
        order = _order_data(wrapper)
        status = _status(order)
        if status == "filled":
            self.store.transition_order_chain(
                chain_id,
                "FILLED",
                attempt_id=attempt_id,
                broker_status=status,
                detail=order,
            )
            if not risk_off:
                self.store.transition_strategy_run(
                    run_id, "POSITION_OPEN", "CANCEL_FILL_RACE", {"order_id": broker_order_id}
                )
            return _execution_result("RISK_OFF" if risk_off else "POSITION_OPEN", order, True)
        if status in TERMINAL_ZERO_FILL_STATUSES:
            self.store.transition_order_chain(
                chain_id,
                "CANCELED" if status in {"canceled", "cancelled"} else status.upper(),
                attempt_id=attempt_id,
                broker_status=status,
                detail=order,
            )
            self.store.transition_strategy_run(
                run_id,
                "FLAT" if risk_off else "NO_TRADE",
                "ENTRY_TERMINAL_ZERO_FILL",
                {"status": status},
            )
            return _execution_result("FLAT" if risk_off else "NO_TRADE", order, True)
        return _execution_result("RISK_OFF" if risk_off else "CANCEL_PENDING", order, True)

    async def reconcile_entry_order(
        self,
        *,
        run_id: str,
        chain_id: str,
        attempt_id: str,
        client_order_id: str,
    ) -> ExecutionResult | None:
        order = await self._lookup_by_client_id(client_order_id)
        if order is None:
            return None
        return self._apply_entry_order(
            run_id, chain_id, attempt_id, order, reconciled=True
        )

    async def reconcile_exit_order(
        self,
        *,
        run_id: str,
        chain_id: str,
        attempt_id: str,
        client_order_id: str,
    ) -> ExecutionResult | None:
        order = await self._lookup_by_client_id(client_order_id)
        if order is None:
            return None
        return self._apply_exit_order(run_id, chain_id, attempt_id, order)

    async def submit_exit(
        self,
        *,
        run_id: str,
        intent_id: str,
        chain_id: str,
        attempt_id: str,
        arguments: dict[str, Any],
        now: datetime | None = None,
    ) -> ExecutionResult:
        validate_mleg_arguments(arguments, action="exit")
        if not self.store.order_intent_matches(intent_id, arguments):
            raise PolicyError("exit arguments do not match the durable order intent")
        snapshot = await self.read_broker_snapshot(run_id=run_id, now=now)
        if not self.settings.execution_enabled or self.settings.read_only:
            raise PolicyError("exit execution is disarmed")
        if not snapshot.market_is_open:
            raise PolicyError("exit requires an open regular market session")
        if snapshot.is_flat:
            self.store.transition_strategy_run(
                run_id, "FLAT", "BROKER_ALREADY_FLAT", {"observed_at": snapshot.observed_at.isoformat()}
            )
            return ExecutionResult("FLAT", None, None, False, {"already_flat": True})

        self.store.transition_strategy_run(
            run_id, "EXIT_SUBMITTING", "MANDATORY_EXIT", {"intent_id": intent_id}
        )
        self.store.transition_order_chain(chain_id, "SUBMITTING")
        self.store.record_order_attempt(
            attempt_id,
            chain_id=chain_id,
            sequence=0,
            client_order_id=str(arguments["client_order_id"]),
            request=arguments,
        )
        permit = make_system_permit(
            tool_name="place_option_order",
            purpose=MutationPurpose.EXIT,
            intent_id=intent_id,
            arguments=arguments,
            now=snapshot.observed_at,
        )
        try:
            wrapper = await self.connection.call_mutation(
                "place_option_order", arguments, permit=permit, principal="system"
            )
            order = _order_data(wrapper)
        except Exception as exc:
            order = await self._lookup_by_client_id(str(arguments["client_order_id"]))
            if order is None:
                self.store.transition_order_chain(
                    chain_id,
                    "UNKNOWN",
                    attempt_id=attempt_id,
                    detail={"error_type": type(exc).__name__},
                )
                self.store.transition_strategy_run(
                    run_id, "RISK_OFF", "AMBIGUOUS_EXIT", {"error_type": type(exc).__name__}
                )
                return ExecutionResult(
                    "RISK_OFF", None, None, True, {"error_type": type(exc).__name__}
                )
        return self._apply_exit_order(run_id, chain_id, attempt_id, order)

    async def _reconcile_ambiguous_entry(
        self,
        *,
        run_id: str,
        chain_id: str,
        attempt_id: str,
        client_order_id: str,
        original_error: Exception,
    ) -> ExecutionResult:
        order = await self._lookup_by_client_id(client_order_id)
        if order is not None:
            return self._apply_entry_order(
                run_id, chain_id, attempt_id, order, reconciled=True
            )
        self.store.transition_order_chain(
            chain_id,
            "UNKNOWN",
            attempt_id=attempt_id,
            detail={"error_type": type(original_error).__name__},
        )
        self.store.transition_strategy_run(
            run_id,
            "RISK_OFF",
            "AMBIGUOUS_ENTRY",
            {"client_order_id": client_order_id, "error_type": type(original_error).__name__},
        )
        return ExecutionResult(
            "RISK_OFF",
            None,
            None,
            True,
            {"error_type": type(original_error).__name__, "blind_retry": False},
        )

    async def _lookup_by_client_id(
        self, client_order_id: str
    ) -> dict[str, Any] | None:
        try:
            wrapper = await self.connection.call_system_read(
                "get_order_by_client_id", {"client_order_id": client_order_id}
            )
            return _order_data(wrapper)
        except Exception:
            return None

    def _apply_entry_order(
        self,
        run_id: str,
        chain_id: str,
        attempt_id: str,
        order: dict[str, Any],
        *,
        reconciled: bool,
    ) -> ExecutionResult:
        status = _status(order)
        broker_id = _broker_id(order)
        if broker_id:
            self.store.bind_broker_order_id(attempt_id, broker_id)
        self.store.record_order_status(
            chain_id, status, detail=order, attempt_id=attempt_id
        )
        if status == "filled":
            chain_state, run_state = "FILLED", "POSITION_OPEN"
        elif status in TERMINAL_ZERO_FILL_STATUSES:
            chain_state, run_state = _terminal_chain_state(status), "NO_TRADE"
        else:
            chain_state, run_state = "PENDING", "ORDER_PENDING"
        self.store.transition_order_chain(
            chain_id,
            chain_state,
            attempt_id=attempt_id,
            broker_status=status,
            detail={"reconciled": reconciled},
        )
        current = self.store.get_strategy_run(run_id)
        if current is None:
            raise ExecutionError("strategy run disappeared during entry reconciliation")
        if current["state"] == "RISK_OFF":
            return ExecutionResult("RISK_OFF", status, broker_id, reconciled, order)
        if run_state == "NO_TRADE" and current["state"] == "ORDER_PENDING":
            self.store.transition_strategy_run(
                run_id, "CANCEL_PENDING", "BROKER_TERMINAL", {"broker_status": status}
            )
        self.store.transition_strategy_run(
            run_id,
            run_state,
            "ENTRY_BROKER_STATUS",
            {"broker_status": status, "reconciled": reconciled},
        )
        return ExecutionResult(run_state, status, broker_id, reconciled, order)

    def _apply_chain_order(
        self,
        chain_id: str,
        attempt_id: str,
        order: dict[str, Any],
        *,
        reconciled: bool,
    ) -> ExecutionResult:
        status = _status(order)
        broker_id = _broker_id(order)
        if broker_id:
            self.store.bind_broker_order_id(attempt_id, broker_id)
        state = (
            "FILLED"
            if status == "filled"
            else _terminal_chain_state(status)
            if status in TERMINAL_ZERO_FILL_STATUSES
            else "PENDING"
        )
        self.store.transition_order_chain(
            chain_id,
            state,
            attempt_id=attempt_id,
            broker_status=status,
            detail=order,
        )
        return ExecutionResult(state, status, broker_id, reconciled, order)

    def _apply_exit_order(
        self,
        run_id: str,
        chain_id: str,
        attempt_id: str,
        order: dict[str, Any],
    ) -> ExecutionResult:
        status = _status(order)
        broker_id = _broker_id(order)
        awaiting_position_reconciliation = status == "filled"
        status_detail = (
            {**order, "awaiting_position_reconciliation": True}
            if awaiting_position_reconciliation
            else order
        )
        if broker_id:
            self.store.bind_broker_order_id(attempt_id, broker_id)
        self.store.record_order_status(
            chain_id, status, detail=status_detail, attempt_id=attempt_id
        )
        if status == "filled":
            # An order-level fill is not proof that Alpaca's position view is
            # flat.  Keep the strategy active until a later broker snapshot
            # independently confirms both zero positions and zero open orders.
            chain_state, run_state = "FILLED", "EXIT_PENDING"
        elif status in TERMINAL_ZERO_FILL_STATUSES:
            chain_state, run_state = _terminal_chain_state(status), "RISK_OFF"
        else:
            chain_state, run_state = "PENDING", "EXIT_PENDING"
        self.store.transition_order_chain(
            chain_id,
            chain_state,
            attempt_id=attempt_id,
            broker_status=status,
            detail=status_detail,
        )
        current = self.store.get_strategy_run(run_id)
        if current is None:
            raise ExecutionError("strategy run disappeared during exit reconciliation")
        if current["state"] == "RISK_OFF":
            return ExecutionResult(
                "RISK_OFF",
                status,
                broker_id,
                False,
                status_detail,
            )
        self.store.transition_strategy_run(
            run_id,
            run_state,
            "EXIT_BROKER_STATUS",
            {
                "broker_status": status,
                "awaiting_position_reconciliation": awaiting_position_reconciliation,
            },
        )
        return ExecutionResult(run_state, status, broker_id, False, status_detail)


def _object_data(wrapper: dict[str, Any]) -> dict[str, Any]:
    data = unwrap_data(wrapper)
    if isinstance(data, dict):
        nested = data.get("result")
        if isinstance(nested, dict):
            return nested
        return data
    raise ExecutionError("MCP response did not contain an object payload")


def _result_list(wrapper: dict[str, Any]) -> list[dict[str, Any]]:
    data = unwrap_data(wrapper)
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict):
        values = next(
            (
                data[key]
                for key in ("result", "orders", "positions", "data")
                if isinstance(data.get(key), list)
            ),
            None,
        )
        if values is None:
            raise ExecutionError("MCP response did not contain a result array")
    else:
        raise ExecutionError("MCP response did not contain a result array")
    return [item for item in values if isinstance(item, dict)]


def _order_data(wrapper: dict[str, Any]) -> dict[str, Any]:
    data = unwrap_data(wrapper)
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict) or not (data.get("id") or data.get("status")):
        raise ExecutionError("MCP order response did not contain an order")
    return data


def _options_level(account: dict[str, Any], config: dict[str, Any]) -> int:
    for payload in (account, config):
        for key in ("options_trading_level", "options_approved_level"):
            value = payload.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
    return 0


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ExecutionError(f"{label} is missing or invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ExecutionError(f"{label} is missing or invalid")
    return parsed


def _status(order: dict[str, Any]) -> str:
    status = str(order.get("status") or "").strip().lower()
    if not status:
        raise ExecutionError("broker order status is missing")
    return status


def _broker_id(order: dict[str, Any]) -> str | None:
    value = order.get("id")
    return str(value) if value else None


def _terminal_chain_state(status: str) -> str:
    return {
        "canceled": "CANCELED",
        "cancelled": "CANCELED",
        "expired": "EXPIRED",
        "rejected": "REJECTED",
        "suspended": "REJECTED",
    }[status]


def _execution_result(
    state: str, order: dict[str, Any], reconciled: bool
) -> ExecutionResult:
    return ExecutionResult(state, _status(order), _broker_id(order), reconciled, order)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _broker_flag(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}


def _entry_authorization_blocker(
    authorization: dict[str, Any] | None, observed_at: datetime
) -> str | None:
    if authorization is None:
        return "ENTRY_AUTHORIZATION_MISSING"
    state = str(authorization.get("state") or "").upper()
    if state != "ARMED":
        return f"ENTRY_AUTHORIZATION_{state or 'INVALID'}"
    try:
        armed_at = datetime.fromisoformat(
            str(authorization["armed_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        expires_at = datetime.fromisoformat(
            str(authorization["expires_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return "ENTRY_AUTHORIZATION_INVALID"
    current = observed_at.astimezone(UTC)
    if current < armed_at:
        return "ENTRY_AUTHORIZATION_NOT_ACTIVE"
    if current >= expires_at:
        return "ENTRY_AUTHORIZATION_EXPIRED"
    return None


async def quote_atomic_exit(
    connection: MCPConnection,
    entry_arguments: dict[str, Any],
    *,
    now: datetime | None = None,
    maximum_age_seconds: int = 60,
) -> ExitQuote:
    """Price the exact opposite four-leg close from indicative MCP snapshots."""

    validate_mleg_arguments(entry_arguments, action="entry")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    symbols = [str(leg["symbol"]) for leg in entry_arguments["legs"]]
    wrapper = await connection.call_system_read(
        "get_option_snapshot",
        {"symbols": ",".join(symbols), "feed": "indicative", "limit": 100},
    )
    data = unwrap_data(wrapper)
    snapshots = data.get("snapshots") if isinstance(data, dict) else None
    if not isinstance(snapshots, dict):
        raise ExecutionError("option snapshot response omitted snapshots")
    midpoint_debit = Decimal("0")
    natural_debit = Decimal("0")
    timestamps: list[datetime] = []
    for leg in entry_arguments["legs"]:
        snapshot = snapshots.get(leg["symbol"])
        if not isinstance(snapshot, dict):
            raise ExecutionError("exit snapshot omitted one or more option legs")
        quote = snapshot.get("latestQuote") or snapshot.get("latest_quote")
        if not isinstance(quote, dict):
            raise ExecutionError("exit snapshot omitted a latest quote")
        bid = _decimal(quote.get("bp", quote.get("bid_price")), "option bid")
        ask = _decimal(quote.get("ap", quote.get("ask_price")), "option ask")
        if ask < bid or bid < 0:
            raise ExecutionError("exit option quote is crossed or invalid")
        timestamp = _timestamp_value(quote.get("t", quote.get("timestamp")))
        age = (observed_at - timestamp).total_seconds()
        if age < -2 or age > maximum_age_seconds:
            raise ExecutionError("exit option quote is stale")
        timestamps.append(timestamp)
        midpoint = (bid + ask) / Decimal("2")
        if leg["side"] == "sell":
            # Short opening leg: buy it back.
            midpoint_debit += midpoint
            natural_debit += ask
        else:
            # Long opening leg: sale proceeds reduce close cost.
            midpoint_debit -= midpoint
            natural_debit -= bid
    positive_midpoint = max(midpoint_debit, Decimal("0.05"))
    proposed, tick = round_debit_up(positive_midpoint, None)
    return ExitQuote(
        observed_at=observed_at,
        midpoint_debit=midpoint_debit,
        natural_debit=max(natural_debit, Decimal("0")),
        proposed_debit=proposed,
        tick_size=tick,
        oldest_quote_at=min(timestamps),
    )


def _timestamp_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExecutionError("option quote timestamp is invalid") from exc
    else:
        raise ExecutionError("option quote timestamp is missing")
    if parsed.tzinfo is None:
        raise ExecutionError("option quote timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _activity_time(activity: dict[str, Any], fallback: datetime) -> datetime:
    for key in ("transaction_time", "occurred_at", "created_at", "date"):
        value = activity.get(key)
        if not value:
            continue
        text = str(value)
        if len(text) == 10:
            text += "T00:00:00+00:00"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    return fallback
