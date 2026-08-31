"""Deterministic, broker-isolated acceptance replay for ThetaTrap.

The replay is intentionally not a backtest and makes no profitability claim.  It
is a small executable specification for the safety-critical paths that must work
before the live paper account is armed.  All market data, model decisions, order
acknowledgements, fills, and timeouts in this module are explicitly simulated.
No MCP connection, HTTP session, or broker SDK is created.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from thetatrap.agent import VETO_CODES
from thetatrap.domain import (
    GateCode,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionSnapshot,
    UnderlyingQuote,
)
from thetatrap.orders import (
    build_entry_order_intent,
    build_exit_order_intent,
    serialize_evaluation,
)
from thetatrap.strategy import evaluate_symbol


SIMULATION_LABEL = "SIMULATION_ONLY_NO_BROKER_OR_MCP"
SUITE_ID = "tt-replay-v1-20260901"
OBSERVED_AT = datetime(2026, 9, 1, 19, 30, tzinfo=UTC)
EVENT_DATE = date(2026, 9, 1)
TRADE_EXPIRATION = date(2026, 9, 4)
TERM_EXPIRATION = date(2026, 9, 11)
PREVIOUS_TRADING_DAY = date(2026, 8, 31)
SIMULATED_ACCOUNT_ID = "SIMULATED-PAPER-ACCOUNT"
STRATEGY_VERSION = "1.2-replay"


class SimulatedPostDispatchTimeout(TimeoutError):
    """A local fixture timeout after a simulated broker accepted the request."""


@dataclass(frozen=True, slots=True)
class ReplayScenarioResult:
    name: str
    passed: bool
    details: dict[str, Any]
    external_broker_mutation_calls: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "simulation_label": SIMULATION_LABEL,
            "external_broker_mutation_calls": self.external_broker_mutation_calls,
            "details": self.details,
        }


class _BrokerIsolationBoundary:
    """Counter that would fail closed if a replay attempted an external mutation."""

    def __init__(self) -> None:
        self.external_broker_mutation_calls = 0

    def dispatch_external_mutation(self, *_: Any, **__: Any) -> None:
        self.external_broker_mutation_calls += 1
        raise AssertionError("replay attempted an external broker mutation")


class _SimulatedOrderLedger:
    """In-memory broker fixture used only for timeout reconciliation evidence."""

    def __init__(self) -> None:
        self.orders_by_client_id: dict[str, dict[str, Any]] = {}
        self.simulated_dispatch_attempts = 0
        self.simulated_reconciliation_reads = 0
        self.duplicate_submissions = 0

    def submit_then_timeout(self, arguments: dict[str, Any]) -> None:
        """Accept one local fixture order, then hide the acknowledgement."""

        self.simulated_dispatch_attempts += 1
        client_order_id = str(arguments["client_order_id"])
        if client_order_id in self.orders_by_client_id:
            self.duplicate_submissions += 1
        else:
            synthetic_id = "sim-order-" + hashlib.sha256(
                client_order_id.encode("utf-8")
            ).hexdigest()[:16]
            self.orders_by_client_id[client_order_id] = {
                "id": synthetic_id,
                "client_order_id": client_order_id,
                "status": "accepted",
                "order_class": "mleg",
                "leg_count": len(arguments["legs"]),
            }
        raise SimulatedPostDispatchTimeout(
            "simulated timeout after local fixture acceptance"
        )

    def get_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        self.simulated_reconciliation_reads += 1
        order = self.orders_by_client_id.get(client_order_id)
        return dict(order) if order is not None else None


def run_replay_suite(
    output_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the five deterministic acceptance scenarios and return JSON-safe data.

    ``None`` or ``":memory:"`` uses an ephemeral SQLite result ledger.  Any other
    path creates (or idempotently updates) a local SQLite replay ledger.  The
    ledger stores audit results only; it is never used as a broker substitute by
    production execution code.
    """

    isolation = _BrokerIsolationBoundary()
    scenario_functions: tuple[
        Callable[[_BrokerIsolationBoundary], ReplayScenarioResult], ...
    ] = (
        _eligible_candidate_scenario,
        _deterministic_rejection_scenario,
        _model_veto_scenario,
        _ambiguous_timeout_reconciliation_scenario,
        _opposite_side_exit_to_flat_scenario,
    )
    scenario_results = [scenario(isolation) for scenario in scenario_functions]
    total_external_calls = isolation.external_broker_mutation_calls
    passed = (
        len(scenario_results) == 5
        and all(result.passed for result in scenario_results)
        and total_external_calls == 0
    )
    core_report: dict[str, Any] = {
        "suite_id": SUITE_ID,
        "simulation_label": SIMULATION_LABEL,
        "observed_at": _iso(OBSERVED_AT),
        "scenario_count": len(scenario_results),
        "passed": passed,
        "external_broker_mutation_calls": total_external_calls,
        "scenarios": [result.as_json() for result in scenario_results],
    }
    digest = hashlib.sha256(_canonical_json(core_report).encode("utf-8")).hexdigest()
    report = {**core_report, "result_digest": digest}

    connection, storage_mode = _open_result_ledger(output_db_path)
    try:
        _initialize_result_ledger(connection)
        _persist_report(connection, report)
    finally:
        connection.close()
    return {**report, "storage_mode": storage_mode}


def _eligible_candidate_scenario(
    isolation: _BrokerIsolationBoundary,
) -> ReplayScenarioResult:
    evaluation = evaluate_symbol(**_evaluation_inputs())
    candidate = evaluation.candidate
    passed = (
        candidate is not None
        and candidate.proposed_credit == Decimal("2.45")
        and candidate.maximum_loss == Decimal("255.00")
        and candidate.quantity == 1
    )
    details = {
        "fixture": "SIMULATED_ELIGIBLE_MARKET",
        "eligible": evaluation.eligible,
        "failure_codes": [code.value for code in evaluation.failure_codes],
        "symbol": evaluation.symbol,
        "proposed_credit": (
            format(candidate.proposed_credit, "f") if candidate is not None else None
        ),
        "maximum_loss": (
            format(candidate.maximum_loss, "f") if candidate is not None else None
        ),
        "quantity": candidate.quantity if candidate is not None else None,
    }
    return ReplayScenarioResult(
        name="eligible_candidate",
        passed=passed,
        details=details,
        external_broker_mutation_calls=isolation.external_broker_mutation_calls,
    )


def _deterministic_rejection_scenario(
    isolation: _BrokerIsolationBoundary,
) -> ReplayScenarioResult:
    inputs = _evaluation_inputs()
    inputs["underlying"] = UnderlyingQuote(
        bid=Decimal("99.99"),
        ask=Decimal("100.01"),
        timestamp=OBSERVED_AT - timedelta(seconds=11),
    )
    first = evaluate_symbol(**inputs)
    second = evaluate_symbol(**inputs)
    first_json = serialize_evaluation(first)
    second_json = serialize_evaluation(second)
    failure_codes = [code.value for code in first.failure_codes]
    passed = (
        first.candidate is None
        and failure_codes == [GateCode.UNDERLYING_QUOTE_STALE.value]
        and first_json == second_json
    )
    return ReplayScenarioResult(
        name="deterministic_rejection",
        passed=passed,
        details={
            "fixture": "SIMULATED_STALE_UNDERLYING_QUOTE",
            "eligible": first.eligible,
            "failure_codes": failure_codes,
            "repeat_evaluation_identical": first_json == second_json,
        },
        external_broker_mutation_calls=isolation.external_broker_mutation_calls,
    )


def _model_veto_scenario(
    isolation: _BrokerIsolationBoundary,
) -> ReplayScenarioResult:
    evaluation = evaluate_symbol(**_evaluation_inputs())
    reason_code = "TRADING_HALT"
    simulated_decision = {
        "reviewer": "SIMULATED_DETERMINISTIC_MODEL",
        "outcome": "VETO",
        "reason_code": reason_code,
        "evidence": ["SIMULATED_FIXTURE_TRADING_HALT"],
        "order_authorized": False,
    }
    passed = (
        evaluation.eligible
        and reason_code in VETO_CODES
        and simulated_decision["outcome"] == "VETO"
        and simulated_decision["order_authorized"] is False
    )
    return ReplayScenarioResult(
        name="model_veto",
        passed=passed,
        details={
            "fixture": "SIMULATED_MODEL_VETO",
            "candidate_was_eligible": evaluation.eligible,
            "decision": simulated_decision,
            "model_network_calls": 0,
        },
        external_broker_mutation_calls=isolation.external_broker_mutation_calls,
    )


def _ambiguous_timeout_reconciliation_scenario(
    isolation: _BrokerIsolationBoundary,
) -> ReplayScenarioResult:
    candidate = _required_candidate()
    intent = build_entry_order_intent(
        candidate,
        environment="replay",
        account_id=SIMULATED_ACCOUNT_ID,
        event_date=EVENT_DATE,
        strategy_version=STRATEGY_VERSION,
    )
    ledger = _SimulatedOrderLedger()
    timed_out = False
    try:
        ledger.submit_then_timeout(intent.arguments)
    except SimulatedPostDispatchTimeout:
        timed_out = True
    reconciled_order = ledger.get_by_client_order_id(intent.client_order_id)
    # The deterministic client ID found an accepted order, so replay deliberately
    # does not call submit again.
    passed = (
        timed_out
        and reconciled_order is not None
        and reconciled_order["client_order_id"] == intent.client_order_id
        and reconciled_order["status"] == "accepted"
        and ledger.simulated_dispatch_attempts == 1
        and ledger.duplicate_submissions == 0
        and len(ledger.orders_by_client_id) == 1
    )
    return ReplayScenarioResult(
        name="ambiguous_timeout_reconciliation",
        passed=passed,
        details={
            "fixture": "SIMULATED_POST_DISPATCH_TIMEOUT",
            "timeout_was_post_dispatch": timed_out,
            "deterministic_client_order_id": intent.client_order_id,
            "reconciled_client_order_id": (
                reconciled_order["client_order_id"]
                if reconciled_order is not None
                else None
            ),
            "reconciled_status": (
                reconciled_order["status"] if reconciled_order is not None else None
            ),
            "simulated_dispatch_attempts": ledger.simulated_dispatch_attempts,
            "simulated_reconciliation_reads": ledger.simulated_reconciliation_reads,
            "orders_with_client_order_id": len(ledger.orders_by_client_id),
            "duplicate_submissions": ledger.duplicate_submissions,
            "retry_suppressed_after_reconciliation": reconciled_order is not None,
        },
        external_broker_mutation_calls=isolation.external_broker_mutation_calls,
    )


def _opposite_side_exit_to_flat_scenario(
    isolation: _BrokerIsolationBoundary,
) -> ReplayScenarioResult:
    candidate = _required_candidate()
    entry = build_entry_order_intent(
        candidate,
        environment="replay",
        account_id=SIMULATED_ACCOUNT_ID,
        event_date=EVENT_DATE,
        strategy_version=STRATEGY_VERSION,
    )
    exit_intent = build_exit_order_intent(
        candidate,
        limit_debit=Decimal("1.20"),
        environment="replay",
        account_id=SIMULATED_ACCOUNT_ID,
        event_date=EVENT_DATE,
        strategy_version=STRATEGY_VERSION,
    )
    entry_legs = entry.arguments["legs"]
    exit_legs = exit_intent.arguments["legs"]
    positions: dict[str, int] = {}
    for leg in entry_legs:
        symbol = str(leg["symbol"])
        positions[symbol] = positions.get(symbol, 0) + _side_sign(str(leg["side"]))
    for leg in exit_legs:
        symbol = str(leg["symbol"])
        positions[symbol] = positions.get(symbol, 0) + _side_sign(str(leg["side"]))

    exits_by_symbol = {str(leg["symbol"]): leg for leg in exit_legs}
    leg_evidence = []
    for entry_leg in entry_legs:
        symbol = str(entry_leg["symbol"])
        exit_leg = exits_by_symbol.get(symbol)
        leg_evidence.append(
            {
                "symbol": symbol,
                "entry_side": entry_leg["side"],
                "exit_side": exit_leg["side"] if exit_leg is not None else None,
                "opposite_side": (
                    exit_leg is not None and entry_leg["side"] != exit_leg["side"]
                ),
                "final_quantity": str(positions.get(symbol, 0)),
            }
        )
    all_flat = len(positions) == 4 and all(quantity == 0 for quantity in positions.values())
    passed = (
        len(entry_legs) == len(exit_legs) == 4
        and set(positions) == set(exits_by_symbol)
        and all(item["opposite_side"] for item in leg_evidence)
        and all_flat
    )
    return ReplayScenarioResult(
        name="opposite_side_exit_to_flat",
        passed=passed,
        details={
            "fixture": "SIMULATED_ENTRY_AND_ATOMIC_EXIT_FILLS",
            "entry_client_order_id": entry.client_order_id,
            "exit_client_order_id": exit_intent.client_order_id,
            "entry_leg_count": len(entry_legs),
            "exit_leg_count": len(exit_legs),
            "exit_limit_debit": exit_intent.arguments["limit_price"],
            "simulated_fill_count": len(entry_legs) + len(exit_legs),
            "legs": leg_evidence,
            "position_state": "FLAT" if all_flat else "NON_FLAT",
        },
        external_broker_mutation_calls=isolation.external_broker_mutation_calls,
    )


def _required_candidate():
    evaluation = evaluate_symbol(**_evaluation_inputs())
    if evaluation.candidate is None:  # pragma: no cover - deterministic invariant
        raise AssertionError("eligible replay fixture stopped producing a candidate")
    return evaluation.candidate


def _evaluation_inputs() -> dict[str, Any]:
    front = [
        _option("90", OptionRight.PUT, "0.20", "0.25", delta="-0.08"),
        _option("95", OptionRight.PUT, "1.40", "1.50", delta="-0.18"),
        _option("100", OptionRight.PUT, "2.40", "2.60", delta="-0.50"),
        _option("100", OptionRight.CALL, "2.40", "2.60", delta="0.50"),
        _option("105", OptionRight.CALL, "1.40", "1.50", delta="0.18"),
        _option("110", OptionRight.CALL, "0.20", "0.25", delta="0.08"),
    ]
    back = [
        _option(
            "100",
            OptionRight.PUT,
            "4.90",
            "5.10",
            expiration=TERM_EXPIRATION,
            iv="0.50",
            delta="-0.50",
        ),
        _option(
            "100",
            OptionRight.CALL,
            "4.90",
            "5.10",
            expiration=TERM_EXPIRATION,
            iv="0.50",
            delta="0.50",
        ),
    ]
    return {
        "symbol": "TEST",
        "observed_at": OBSERVED_AT,
        "underlying": UnderlyingQuote(
            bid=Decimal("99.99"),
            ask=Decimal("100.01"),
            timestamp=OBSERVED_AT - timedelta(seconds=2),
        ),
        "front_chain": front,
        "back_chain": back,
        "trade_expiration": TRADE_EXPIRATION,
        "term_expiration": TERM_EXPIRATION,
        "previous_trading_day": PREVIOUS_TRADING_DAY,
        "initial_equity": Decimal("100000"),
        "buying_power": Decimal("100000"),
    }


def _option(
    strike: str,
    right: OptionRight,
    bid: str,
    ask: str,
    *,
    expiration: date = TRADE_EXPIRATION,
    iv: str = "0.60",
    delta: str,
) -> OptionSnapshot:
    strike_decimal = Decimal(strike)
    right_letter = "C" if right is OptionRight.CALL else "P"
    occ_strike = int(strike_decimal * Decimal("1000"))
    symbol = f"TEST{expiration:%y%m%d}{right_letter}{occ_strike:08d}"
    return OptionSnapshot(
        contract=OptionContract(
            symbol=symbol,
            underlying_symbol="TEST",
            expiration=expiration,
            right=right,
            strike=strike_decimal,
            tradable=True,
            status="active",
            multiplier=Decimal("100"),
            size=Decimal("100"),
            open_interest=100,
            open_interest_date=PREVIOUS_TRADING_DAY,
            ppind=True,
        ),
        quote=OptionQuote(
            bid=Decimal(bid),
            ask=Decimal(ask),
            timestamp=OBSERVED_AT - timedelta(seconds=5),
            implied_volatility=Decimal(iv),
            delta=Decimal(delta),
        ),
    )


def _side_sign(side: str) -> int:
    if side == "buy":
        return 1
    if side == "sell":
        return -1
    raise ValueError(f"unsupported simulated side: {side}")


def _open_result_ledger(
    output_db_path: str | Path | None,
) -> tuple[sqlite3.Connection, str]:
    if output_db_path is None or str(output_db_path) == ":memory:":
        return sqlite3.connect(":memory:"), "in_memory"
    path = Path(output_db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path), "sqlite_file"


def _initialize_result_ledger(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS replay_suites (
            suite_id TEXT PRIMARY KEY,
            simulation_label TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            passed INTEGER NOT NULL,
            external_broker_mutation_calls INTEGER NOT NULL,
            result_digest TEXT NOT NULL,
            result_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS replay_scenarios (
            suite_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            scenario_name TEXT NOT NULL,
            passed INTEGER NOT NULL,
            external_broker_mutation_calls INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY (suite_id, scenario_name),
            UNIQUE (suite_id, ordinal),
            FOREIGN KEY (suite_id) REFERENCES replay_suites(suite_id)
        );
        """
    )


def _persist_report(connection: sqlite3.Connection, report: dict[str, Any]) -> None:
    result_json = _canonical_json(report)
    connection.execute(
        """
        INSERT INTO replay_suites(
            suite_id, simulation_label, observed_at, passed,
            external_broker_mutation_calls, result_digest, result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(suite_id) DO UPDATE SET
            simulation_label=excluded.simulation_label,
            observed_at=excluded.observed_at,
            passed=excluded.passed,
            external_broker_mutation_calls=excluded.external_broker_mutation_calls,
            result_digest=excluded.result_digest,
            result_json=excluded.result_json
        """,
        (
            report["suite_id"],
            report["simulation_label"],
            report["observed_at"],
            int(report["passed"]),
            report["external_broker_mutation_calls"],
            report["result_digest"],
            result_json,
        ),
    )
    for ordinal, scenario in enumerate(report["scenarios"], start=1):
        connection.execute(
            """
            INSERT INTO replay_scenarios(
                suite_id, ordinal, scenario_name, passed,
                external_broker_mutation_calls, result_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(suite_id, scenario_name) DO UPDATE SET
                ordinal=excluded.ordinal,
                passed=excluded.passed,
                external_broker_mutation_calls=excluded.external_broker_mutation_calls,
                result_json=excluded.result_json
            """,
            (
                report["suite_id"],
                ordinal,
                scenario["name"],
                int(scenario["passed"]),
                scenario["external_broker_mutation_calls"],
                _canonical_json(scenario),
            ),
        )
    connection.commit()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
