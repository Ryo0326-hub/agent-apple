"""Public, read-only evidence dashboard with no operator mutation imports."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from thetatrap.report import ReportUnavailable, build_operational_report


BUILD_SHA_ENV = "THETATRAP_BUILD_SHA"
HEARTBEAT_STALE_SECONDS = 180
REDACTED = "[REDACTED]"
IDENTIFIER_FAMILIES = (
    "account",
    "authorization",
    "attempt",
    "candidate",
    "chain",
    "fill",
    "intent",
    "order",
    "run",
    "session",
    "snapshot",
)
UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
PAPER_ACCOUNT_PATTERN = re.compile(r"\bPA[A-Z0-9]{8,}\b")
OPTION_SYMBOL_PATTERN = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")
CANARY_STRATEGY_DATE = "2026-09-03"
CANARY_PROFILE_ID = "sep3_intraday_theta_canary_v1"
CANARY_PROFILE = {
    "profile_id": CANARY_PROFILE_ID,
    "name": "Intraday Theta Canary",
    "scope": "September 3 competition run only",
    "universe": "QQQ and SPY, ranked by complete quote quality",
    "expiration": "September 4, 2026 (1 DTE)",
    "entry_window": "09:45–10:45 ET",
    "cancel_at": "10:50 ET",
    "exit_start": "15:15 ET",
    "aggressive_exit_at": "15:25 ET",
    "flat_target": "15:45 ET",
    "structure": "$1-wide symmetric iron condor · 1 contract",
    "minimum_credit": "$0.20",
    "maximum_defined_loss": "$80",
    "qwen_cadence": "eligible review cycle no more than once every 5 minutes",
}


def display_build_sha(raw: str | None = None) -> str:
    value = (raw if raw is not None else os.environ.get(BUILD_SHA_ENV, "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        return "unversioned"
    return value.lower()[:12]


def redact_public_identifiers(value: Any, key: str | None = None) -> Any:
    """Recursively remove account and lifecycle identifiers from public data."""

    if key and _is_private_identifier_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_public_identifiers(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_public_identifiers(item) for item in value]
    if isinstance(value, str):
        if UUID_PATTERN.search(value) or PAPER_ACCOUNT_PATTERN.search(value):
            return REDACTED
    return value


def build_public_view(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project the complete audit report into credential-free public evidence."""

    mode = _mapping(report.get("mode"))
    identity = _mapping(report.get("identity"))
    health = _mapping(report.get("health"))
    mcp = _mapping(report.get("mcp"))
    session = _mapping(mcp.get("latest_session"))
    strategy = _mapping(report.get("strategy"))
    focus_run = _mapping(strategy.get("current_run")) or _mapping(
        strategy.get("last_run")
    )
    candidate_section = _mapping(report.get("candidate"))
    candidate = _mapping(candidate_section.get("selected"))
    agent = _mapping(report.get("agent"))
    latest_agent = _mapping(agent.get("latest_run"))
    orders = _mapping(report.get("orders"))
    portfolio = _mapping(report.get("portfolio"))
    position = _mapping(portfolio.get("latest_position_observation"))
    current_positions = [
        {
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "quantity": item.get("qty") or item.get("quantity"),
            "average_entry_price": item.get("avg_entry_price"),
            "current_price": item.get("current_price"),
            "market_value": item.get("market_value"),
            "unrealized_pnl": item.get("unrealized_pl"),
        }
        for item in map(_mapping, _sequence(position.get("payload")))
    ]
    equity = _mapping(portfolio.get("equity"))
    kill = _mapping(report.get("kill_switch"))
    one_shot = _mapping(report.get("one_shot_entry"))
    safety = _mapping(report.get("safety"))
    data_profile = _mapping(report.get("data_profile"))
    heartbeat_age = _heartbeat_age_seconds(health.get("observed_at"))
    run_history = _sequence(strategy.get("run_history"))
    run_dates = {
        str(run.get("run_id")): run.get("strategy_date")
        for run in map(_mapping, run_history)
        if run.get("run_id")
    }
    strategy_profile = _public_strategy_profile(strategy, focus_run)

    chains: list[dict[str, Any]] = []
    order_timeline: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    for chain_value in _sequence(orders.get("chains")):
        chain = _mapping(chain_value)
        attempts = _sequence(chain.get("attempts"))
        chain_fills = _sequence(chain.get("fills"))
        status_history = _sequence(chain.get("status_history"))
        intent_payload = _mapping(chain.get("payload"))
        intent_legs = [_mapping(leg) for leg in _sequence(intent_payload.get("legs"))]
        broker_fill_observations = [
            _mapping(status)
            for status in status_history
            if str(_mapping(status).get("broker_status") or "").lower() == "filled"
        ]
        broker_fill_observation = next(
            (
                status
                for status in reversed(broker_fill_observations)
                if str(status.get("event_kind") or "").lower() == "broker_observation"
            ),
            broker_fill_observations[-1] if broker_fill_observations else {},
        )
        broker_fill_detail = _mapping(broker_fill_observation.get("detail"))
        broker_fill_confirmed = bool(broker_fill_observation)
        attempt_rows = [_mapping(attempt) for attempt in attempts]
        initial_request = (
            _mapping(attempt_rows[0].get("request")) if attempt_rows else {}
        )
        final_request = (
            _mapping(attempt_rows[-1].get("request")) if attempt_rows else {}
        )
        broker_leg_fills = [
            {
                "symbol": leg.get("symbol"),
                "side": leg.get("side"),
                "position_intent": leg.get("position_intent"),
                "quantity": leg.get("filled_qty"),
                "price": leg.get("filled_avg_price"),
                "filled_at": leg.get("filled_at"),
            }
            for leg in map(_mapping, _sequence(broker_fill_detail.get("legs")))
            if str(leg.get("status") or "").lower() == "filled"
            and leg.get("filled_avg_price") not in (None, "")
        ]
        chains.append(
            {
                "strategy_date": run_dates.get(str(chain.get("run_id"))),
                "symbol": _option_underlying(
                    intent_legs[0].get("symbol") if intent_legs else None
                ),
                "purpose": chain.get("purpose"),
                "state": chain.get("state"),
                "initial_limit_price": initial_request.get("limit_price")
                or intent_payload.get("limit_price"),
                "final_limit_price": final_request.get("limit_price")
                or intent_payload.get("limit_price"),
                "created_at": chain.get("created_at"),
                "submitted_at": (
                    attempt_rows[0].get("created_at") if attempt_rows else None
                ),
                "updated_at": chain.get("updated_at"),
                "broker_filled_at": (
                    broker_fill_detail.get("filled_at")
                    or broker_fill_observation.get("observed_at")
                ),
                "broker_fill_price": broker_fill_detail.get("filled_avg_price"),
                "broker_filled_quantity": broker_fill_detail.get("filled_qty"),
                "broker_fill_confirmed": broker_fill_confirmed,
                "broker_leg_fills": broker_leg_fills,
                "attempt_count": len(attempts),
                "replacement_count": max(0, len(attempts) - 1),
                "fill_count": len(chain_fills),
            }
        )
        for status_value in status_history:
            status = _mapping(status_value)
            order_timeline.append(
                {
                    "observed_at": status.get("observed_at"),
                    "strategy_date": run_dates.get(str(chain.get("run_id"))),
                    "purpose": chain.get("purpose"),
                    "event": status.get("event_kind"),
                    "from": status.get("from_state"),
                    "to": status.get("to_state"),
                    "broker_status": status.get("broker_status"),
                }
            )
        for fill_value in chain_fills:
            fill = _mapping(fill_value)
            fills.append(
                {
                    "filled_at": fill.get("filled_at"),
                    "strategy_date": run_dates.get(str(chain.get("run_id"))),
                    "purpose": chain.get("purpose"),
                    "symbol": fill.get("symbol"),
                    "side": fill.get("side"),
                    "quantity": fill.get("quantity"),
                    "price": fill.get("price"),
                }
            )

    trace: list[dict[str, Any]] = []
    for item_value in _sequence(agent.get("tool_trace")):
        item = _mapping(item_value)
        trace.append(_public_tool_trace(item))

    scan_history = [
        _public_candidate_scan(_mapping(item))
        for item in _sequence(candidate_section.get("history"))
    ]
    scan_matrix = [
        _public_scan_matrix_row(_mapping(item))
        for item in _sequence(candidate_section.get("scan_matrix"))
    ]
    scan_matrix = _augment_scan_matrix(scan_matrix, scan_history)
    reviews = [
        _public_review(_mapping(item), kind="QWEN_DECISION")
        for item in _sequence(agent.get("reviews"))
    ]
    advisories = [
        _public_review(_mapping(item), kind="READ_ONLY_ADVISORY")
        for item in _sequence(agent.get("advisories"))
    ]
    primary_agent_review = _select_primary_execution_review(reviews)
    agent_timeline: list[dict[str, Any]] = []
    for review in reviews + advisories:
        for tool in _sequence(review.get("tool_trace")):
            row = dict(_mapping(tool))
            row.update(
                {
                    "strategy_date": review.get("strategy_date"),
                    "symbol": review.get("symbol"),
                    "review_kind": review.get("kind"),
                }
            )
            agent_timeline.append(row)
    agent_timeline.sort(key=lambda item: str(item.get("called_at") or ""))

    mcp_timeline = [
        {
            "called_at": item.get("called_at"),
            "principal": item.get("principal"),
            "tool": item.get("tool_name"),
            "status": item.get("status"),
            "duration_ms": item.get("duration_ms"),
        }
        for item in map(_mapping, _sequence(mcp.get("timeline")))
    ]
    equity_history = []
    for point_value in _sequence(equity.get("history")):
        point = _mapping(point_value)
        equity_history.append(
            {
                "observed_at": point.get("observed_at"),
                "equity": _number(point.get("equity")),
                "buying_power": _number(point.get("buying_power")),
            }
        )

    transitions = [
        {
            "strategy_date": item.get("strategy_date"),
            "transitioned_at": item.get("transitioned_at"),
            "from": item.get("from_state") or "START",
            "to": item.get("to_state"),
            "reason": item.get("reason_code"),
        }
        for item in map(
            _mapping,
            _sequence(strategy.get("transition_history"))
            or _sequence(strategy.get("transitions")),
        )
    ]
    safety_kill = _mapping(safety.get("kill_switch")) or kill

    broker_filled_order_count = sum(
        1 for chain in chains if chain.get("broker_fill_confirmed") is True
    )
    broker_leg_fills = [
        {"purpose": chain.get("purpose"), **dict(_mapping(fill))}
        for chain in chains
        for fill in _sequence(chain.get("broker_leg_fills"))
    ]
    broker_mleg_cash_flow = _broker_mleg_cash_flow(chains)
    broker_filled_purposes = {
        str(chain.get("purpose") or "").lower()
        for chain in chains
        if chain.get("broker_fill_confirmed") is True
    }
    broker_round_trip_confirmed = {"entry", "exit"}.issubset(
        broker_filled_purposes
    ) and position.get("is_flat") is True
    normalized_fill_count = len(fills)
    public = {
        "mode": {
            "environment": mode.get("environment"),
            "paper": mode.get("paper", True),
            "data_feed": mode.get("data_feed"),
            "trading_state": mode.get("trading_state"),
            "banner": mode.get("banner"),
            "read_only_viewer": safety.get("read_only_viewer", True),
        },
        "account": {"suffix": identity.get("account_suffix", "unverified")},
        "health": {
            "status": health.get("status"),
            "observed_at": health.get("observed_at"),
            "market_is_open": health.get("market_is_open"),
            "heartbeat_age_seconds": heartbeat_age,
            "stale": (heartbeat_age is None or heartbeat_age > HEARTBEAT_STALE_SECONDS),
        },
        "mcp": {
            "status": mcp.get("operational_status") or session.get("status"),
            "package_version": session.get("package_version"),
            "tool_count": session.get("tool_count"),
            "required_schema_hash": session.get("required_schema_hash"),
            "last_successful_call_at": _mapping(mcp.get("last_successful_call")).get(
                "called_at"
            ),
            "timeline": mcp_timeline,
        },
        "emergency": {
            "known": kill.get("known"),
            "enabled": kill.get("enabled"),
        },
        "one_shot_entry": {
            "state": one_shot.get("state", "MISSING"),
            "strategy_date": one_shot.get("strategy_date"),
            "active": one_shot.get("active", False),
        },
        "strategy": {
            "state": focus_run.get("state", "NOT_STARTED"),
            "strategy_date": focus_run.get("strategy_date"),
            "version": focus_run.get("strategy_version"),
            "profile": strategy_profile,
            "no_trade_reason": strategy.get("no_trade_reason"),
            "transitions": transitions,
            "run_count": len(run_history),
        },
        "candidate": {
            "symbol": candidate.get("symbol"),
            "rank": candidate.get("candidate_rank"),
            "eligible": candidate.get("eligible"),
            "payload": candidate.get("payload"),
            "gates": candidate_section.get("latest_gate_outcomes", []),
            "scan_matrix": scan_matrix,
            "history": scan_history,
        },
        "agent": {
            "model": primary_agent_review.get("model") or latest_agent.get("model"),
            "status": primary_agent_review.get("status") or latest_agent.get("status"),
            "decision": (
                primary_agent_review.get("decision")
                or _mapping(latest_agent.get("result")).get("decision")
                or _mapping(latest_agent.get("result")).get("outcome")
                or latest_agent.get("veto_reason")
            ),
            "veto_reason": (
                primary_agent_review.get("reason")
                if primary_agent_review
                else latest_agent.get("veto_reason")
            ),
            "error_type": (
                primary_agent_review.get("reason")
                if primary_agent_review
                and str(primary_agent_review.get("status") or "").upper() == "FAILED"
                else latest_agent.get("error_type")
                if not primary_agent_review
                else None
            ),
            "tool_trace": trace,
            "reviews": reviews,
            "advisories": advisories,
            "timeline": agent_timeline,
        },
        "orders": {
            "chain_count": orders.get("chain_count", 0),
            "fill_count": normalized_fill_count,
            "broker_filled_order_count": broker_filled_order_count,
            "broker_leg_fill_count": len(broker_leg_fills),
            "broker_leg_fills": broker_leg_fills,
            "broker_mleg_cash_flow_ex_fees": broker_mleg_cash_flow,
            "broker_round_trip_confirmed": broker_round_trip_confirmed,
            "fill_evidence": (
                "NORMALIZED_LEG_FILLS"
                if normalized_fill_count
                else "BROKER_ORDER_STATUS"
                if broker_filled_order_count
                else "NONE"
            ),
            "cash_flow_verified": normalized_fill_count > 0,
            "option_cash_flow_ex_fees": orders.get("option_cash_flow_ex_fees", "0.00"),
            "chains": chains,
            "timeline": sorted(
                order_timeline, key=lambda item: str(item.get("observed_at") or "")
            ),
            "fills": sorted(fills, key=lambda item: str(item.get("filled_at") or "")),
        },
        "portfolio": {
            "position": (
                "FLAT"
                if position.get("is_flat") is True
                else "OPEN"
                if position
                else "UNKNOWN"
            ),
            "position_count": len(current_positions),
            "positions": current_positions,
            "first_equity": equity.get("first"),
            "latest_equity": equity.get("latest"),
            "observed_change": equity.get("observed_change"),
            "observed_at": equity.get("latest_observed_at"),
            "equity_history": equity_history,
            "position_history": [
                {
                    "observed_at": item.get("observed_at"),
                    "is_flat": item.get("is_flat"),
                }
                for item in map(_mapping, _sequence(portfolio.get("position_history")))
            ],
        },
        "safety": {
            "kill_switch": {
                "known": safety_kill.get("known"),
                "enabled": safety_kill.get("enabled"),
                "reason": safety_kill.get("reason"),
                "updated_at": safety_kill.get("updated_at"),
                "history": [
                    {
                        "created_at": item.get("created_at"),
                        "enabled": item.get("enabled"),
                        "reason": item.get("reason"),
                        "version": item.get("version"),
                    }
                    for item in map(
                        _mapping, _sequence(safety_kill.get("recent_events"))
                    )
                ],
            },
            "entry_permission": {
                "state": one_shot.get("state", "MISSING"),
                "strategy_date": one_shot.get("strategy_date"),
                "active": one_shot.get("active", False),
            },
            "entry_permission_history": [
                {
                    "strategy_date": item.get("strategy_date"),
                    "state": item.get("state"),
                    "armed_at": item.get("armed_at"),
                    "expires_at": item.get("expires_at"),
                    "consumed_at": item.get("consumed_at"),
                    "revoked_at": item.get("revoked_at"),
                }
                for item in map(_mapping, _sequence(safety.get("entry_permissions")))
            ],
            "maximum_defined_loss": (
                "80.00"
                if strategy_profile.get("profile_id") == CANARY_PROFILE_ID
                else safety.get("maximum_defined_loss", "500.00")
            ),
            "maximum_contracts": safety.get("maximum_contracts", 1),
            "equity_kill_threshold": safety.get("equity_kill_threshold", "99000.00"),
            "paper_only": safety.get("paper_only", True),
            "read_only_viewer": safety.get("read_only_viewer", True),
        },
        "source_labels": report.get("source_labels", {}),
        "data_profile": {
            "profile_id": data_profile.get("profile_id"),
            "provider": data_profile.get("provider"),
            "plan": data_profile.get("plan"),
            "stock_feed": data_profile.get("stock_feed"),
            "option_feed": data_profile.get("option_feed"),
            "consolidated_stock_quotes": data_profile.get(
                "consolidated_stock_quotes", False
            ),
            "consolidated_option_quotes": data_profile.get(
                "consolidated_option_quotes", False
            ),
            "limitations": data_profile.get("limitations", []),
        },
        "limitations": report.get("limitations", []),
        "report_digest": report.get("report_digest"),
    }
    public["competition_days"] = _competition_day_summaries(public)
    public["activity_timeline"] = _build_activity_timeline(public)
    return redact_public_identifiers(public)


def _public_tool_trace(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sequence": item.get("sequence"),
        "turn": item.get("turn"),
        "kind": (
            "official MCP"
            if item.get("is_official_mcp") is True
            else "read-only hash"
            if item.get("arguments_hash") and "is_official_mcp" not in item
            else "local"
        ),
        "tool": item.get("tool_name"),
        "status": item.get("status"),
        "duration_ms": item.get("duration_ms"),
        "arguments": item.get("arguments"),
        "result_summary": item.get("result_summary"),
        "called_at": item.get("called_at"),
    }


def _public_review(item: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    result = _mapping(item.get("result"))
    decision = (
        result.get("assessment")
        if kind == "READ_ONLY_ADVISORY"
        else result.get("decision") or result.get("outcome")
    ) or (
        result.get("assessment")
        or result.get("outcome")
        or item.get("veto_reason")
        or item.get("error_type")
    )
    return {
        "kind": kind,
        "strategy_date": item.get("strategy_date"),
        "symbol": item.get("symbol"),
        "rank": item.get("candidate_rank"),
        "mode": item.get("mode"),
        "model": item.get("model"),
        "status": item.get("status"),
        "decision": decision,
        "reason": item.get("veto_reason") or item.get("error_type"),
        "summary": result.get("summary"),
        "evidence": _sequence(result.get("evidence")),
        "non_authorizing": result.get("non_authorizing"),
        "started_at": item.get("started_at"),
        "ended_at": item.get("ended_at"),
        "tool_trace": [
            _public_tool_trace(_mapping(trace))
            for trace in _sequence(item.get("tool_trace"))
        ],
    }


def _public_candidate_scan(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = _candidate_payload(item.get("payload"))
    gates = [
        {
            "evaluated_at": gate.get("evaluated_at"),
            "gate": gate.get("gate_name"),
            "passed": gate.get("passed"),
            "reason": gate.get("reason_code"),
            "detail": gate.get("detail"),
        }
        for gate in map(_mapping, _sequence(item.get("gates")))
    ]
    failures = _sequence(_mapping(item.get("payload")).get("failures"))
    failed_names = list(_sequence(item.get("failed_gate_names")))
    if not failed_names:
        failed_names = [
            str(_mapping(failure).get("code"))
            for failure in failures
            if _mapping(failure).get("code")
        ]
    return {
        "strategy_date": item.get("strategy_date"),
        "scanned_at": item.get("scanned_at") or item.get("created_at"),
        "symbol": item.get("symbol"),
        "rank": item.get("candidate_rank"),
        "eligible": item.get("eligible"),
        "result": "ELIGIBLE" if item.get("eligible") else "REJECTED",
        "failed_gates": failed_names,
        "spot": payload.get("spot"),
        "iv_ratio": payload.get("iv_ratio"),
        "expected_move": payload.get("expected_move"),
        "expected_move_fraction": payload.get("expected_move_fraction"),
        "proposed_credit": payload.get("proposed_credit"),
        "maximum_loss": payload.get("maximum_loss"),
        "risk_budget": payload.get("risk_budget"),
        "quantity": payload.get("quantity"),
        "legs": _candidate_legs(payload),
        "gates": gates,
        "failure_details": [
            {
                "gate": _mapping(failure).get("code"),
                "detail": _mapping(failure).get("detail"),
            }
            for failure in failures
        ],
    }


def _public_scan_matrix_row(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = _candidate_payload(item.get("latest_payload"))
    return {
        "event_date": item.get("event_date"),
        "symbol": item.get("symbol"),
        "configured": item.get("configured_status") or item.get("status"),
        "latest_result": item.get("latest_result"),
        "evaluations": item.get("evaluation_count", 0),
        "eligible": item.get("eligible_count", 0),
        "latest_scan": item.get("latest_scanned_at"),
        "failed_gate": ", ".join(
            str(value) for value in _sequence(item.get("latest_failed_gates"))
        ),
        "iv_ratio": payload.get("iv_ratio"),
        "max_loss": payload.get("maximum_loss"),
        "exclusion": item.get("exclusion_reason"),
    }


def _augment_scan_matrix(
    matrix: list[dict[str, Any]], history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Include date-scoped canary symbols that are not in the earnings event table."""

    keys = {(row.get("event_date"), row.get("symbol")) for row in matrix}
    grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for item in history:
        key = (item.get("strategy_date"), item.get("symbol"))
        grouped.setdefault(key, []).append(item)
    for key, rows in grouped.items():
        if key in keys:
            continue
        latest = max(rows, key=lambda item: str(item.get("scanned_at") or ""))
        matrix.append(
            {
                "event_date": key[0],
                "symbol": key[1],
                "configured": (
                    "SEP3_CANARY" if key[0] == CANARY_STRATEGY_DATE else "OBSERVED"
                ),
                "latest_result": latest.get("result"),
                "evaluations": len(rows),
                "eligible": sum(1 for item in rows if item.get("eligible") is True),
                "latest_scan": latest.get("scanned_at"),
                "failed_gate": ", ".join(
                    str(value) for value in _sequence(latest.get("failed_gates"))
                ),
                "iv_ratio": latest.get("iv_ratio"),
                "max_loss": latest.get("maximum_loss"),
                "exclusion": None,
            }
        )
    return sorted(
        matrix,
        key=lambda item: (
            str(item.get("event_date") or ""),
            str(item.get("symbol") or ""),
        ),
    )


def _candidate_payload(value: Any) -> Mapping[str, Any]:
    payload = _mapping(value)
    nested = _mapping(payload.get("candidate"))
    return nested or payload


def _candidate_legs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for role in ("long_put", "short_put", "short_call", "long_call"):
        leg = _mapping(payload.get(role))
        snapshot = _mapping(leg.get("snapshot"))
        contract = _mapping(snapshot.get("contract"))
        quote = _mapping(snapshot.get("quote"))
        if not leg:
            continue
        legs.append(
            {
                "role": leg.get("role") or role,
                "side": leg.get("side"),
                "symbol": contract.get("symbol"),
                "strike": contract.get("strike"),
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "quote_time": quote.get("timestamp"),
            }
        )
    return legs


def _public_strategy_profile(
    strategy: Mapping[str, Any], focus_run: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a public-safe description of the profile governing the focus run."""

    context = _mapping(focus_run.get("context"))
    reported = (
        _mapping(strategy.get("profile"))
        or _mapping(focus_run.get("profile"))
        or _mapping(context.get("strategy_profile"))
    )
    profile_id = str(
        reported.get("profile_id")
        or reported.get("id")
        or context.get("strategy_profile_id")
        or ""
    )
    strategy_date = str(focus_run.get("strategy_date") or "")
    is_canary = (
        strategy_date == CANARY_STRATEGY_DATE
        or profile_id == CANARY_PROFILE_ID
        or "intraday_theta_canary" in profile_id.lower()
    )
    if is_canary:
        profile = dict(CANARY_PROFILE)
        for key in profile:
            if reported.get(key) not in (None, ""):
                profile[key] = reported[key]
        entry_window = _mapping(context.get("entry_window_et"))
        exit_window = _mapping(context.get("exit_window_et"))
        structure = _mapping(context.get("structure"))
        symbols = _sequence(context.get("symbols"))
        if context.get("strategy_name"):
            profile["name"] = context.get("strategy_name")
        if symbols:
            profile["universe"] = ", ".join(str(symbol) for symbol in symbols)
        if context.get("expiration"):
            profile["expiration"] = str(context.get("expiration")) + " (1 DTE)"
        if entry_window.get("start") and entry_window.get("stop_new_orders"):
            profile["entry_window"] = (
                f"{entry_window['start']}–{entry_window['stop_new_orders']} ET"
            )
        if entry_window.get("cancel_all_unfilled"):
            profile["cancel_at"] = f"{entry_window['cancel_all_unfilled']} ET"
        if exit_window.get("start"):
            profile["exit_start"] = f"{exit_window['start']} ET"
        if exit_window.get("aggressive_limit"):
            profile["aggressive_exit_at"] = f"{exit_window['aggressive_limit']} ET"
        if exit_window.get("broker_flat_target"):
            profile["flat_target"] = f"{exit_window['broker_flat_target']} ET"
        if structure:
            profile["structure"] = (
                f"${structure.get('wing_width', '1.00')}-wide symmetric iron condor"
                f" · {structure.get('quantity', 1)} contract"
            )
            if structure.get("minimum_credit"):
                profile["minimum_credit"] = f"${structure['minimum_credit']}"
            if structure.get("maximum_loss_dollars"):
                profile["maximum_defined_loss"] = (
                    f"${structure['maximum_loss_dollars']}"
                )
        profile["activation_reason"] = context.get("activation_reason")
        profile["profitability_claim"] = context.get("profitability_claim") or "none"
        profile["profile_kind"] = context.get("profile_kind")
        profile["phase"] = "ADAPTIVE_COMPETITION_PROFILE"
        return profile
    return {
        "profile_id": profile_id or "earnings_theta_trap_v1",
        "name": "Earnings ThetaTrap",
        "scope": "September 1–2 verified earnings events",
        "universe": "Frozen first-party earnings universe",
        "expiration": "September 4, 2026",
        "entry_window": "14:50–15:40 ET",
        "cancel_at": "15:45 ET",
        "exit_start": "Next session, 09:45 ET",
        "structure": "Expected-move iron condor · 1 contract",
        "maximum_defined_loss": "$500",
        "phase": "PRIMARY_EARNINGS_PROFILE",
    }


def _competition_day_summaries(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidate = _mapping(view.get("candidate"))
    agent = _mapping(view.get("agent"))
    orders = _mapping(view.get("orders"))
    dates = {"2026-09-01", "2026-09-02", CANARY_STRATEGY_DATE}
    scans = [_mapping(item) for item in _sequence(candidate.get("history"))]
    reviews = [_mapping(item) for item in _sequence(agent.get("reviews"))]
    advisories = [_mapping(item) for item in _sequence(agent.get("advisories"))]
    chains = [_mapping(item) for item in _sequence(orders.get("chains"))]
    fills = [_mapping(item) for item in _sequence(orders.get("fills"))]

    rows: list[dict[str, Any]] = []
    for strategy_date in sorted(dates):
        day_scans = [
            item for item in scans if item.get("strategy_date") == strategy_date
        ]
        day_reviews = [
            item for item in reviews if item.get("strategy_date") == strategy_date
        ]
        day_advisories = [
            item for item in advisories if item.get("strategy_date") == strategy_date
        ]
        day_chains = [
            item for item in chains if item.get("strategy_date") == strategy_date
        ]
        day_fills = [
            item for item in fills if item.get("strategy_date") == strategy_date
        ]
        broker_filled_orders = sum(
            1 for item in day_chains if item.get("broker_fill_confirmed") is True
        )
        broker_filled_purposes = {
            str(item.get("purpose") or "").lower()
            for item in day_chains
            if item.get("broker_fill_confirmed") is True
        }
        failures = Counter(
            str(gate)
            for scan in day_scans
            for gate in _sequence(scan.get("failed_gates"))
            if gate
        )
        eligible = sum(1 for item in day_scans if item.get("eligible") is True)
        if day_fills:
            outcome = "FILLS RECORDED"
        elif {"entry", "exit"}.issubset(broker_filled_purposes):
            outcome = "BROKER-CONFIRMED ROUND TRIP"
        elif broker_filled_orders:
            outcome = "BROKER-CONFIRMED MLEG FILL"
        elif day_chains:
            outcome = "ORDER LIFECYCLE RECORDED"
        elif day_scans and eligible == 0:
            outcome = "NO TRADE · GATES HELD"
        elif eligible:
            outcome = "ELIGIBLE · NO ORDER CHAIN"
        else:
            outcome = "SCHEDULED"
        rows.append(
            {
                "date": strategy_date,
                "profile": (
                    "Intraday Theta Canary"
                    if strategy_date == CANARY_STRATEGY_DATE
                    else "Earnings ThetaTrap"
                ),
                "outcome": outcome,
                "scans": len(day_scans),
                "eligible": eligible,
                "qwen_decisions": len(day_reviews),
                "qwen_advisories": len(day_advisories),
                "order_chains": len(day_chains),
                "broker_filled_orders": broker_filled_orders,
                "leg_fills": len(day_fills),
                "top_gate_failures": ", ".join(
                    f"{name} ({count})" for name, count in failures.most_common(3)
                ),
            }
        )
    return rows


def _build_activity_timeline(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Merge existing public report evidence into one chronological ledger."""

    rows: list[dict[str, Any]] = []

    def add(
        occurred_at: Any,
        category: str,
        strategy_date: Any,
        subject: Any,
        outcome: Any,
        detail: Any = None,
    ) -> None:
        rows.append(
            {
                "occurred_at": occurred_at,
                "date": strategy_date,
                "category": category,
                "subject": subject,
                "outcome": outcome,
                "detail": detail,
            }
        )

    strategy = _mapping(view.get("strategy"))
    for item in map(_mapping, _sequence(strategy.get("transitions"))):
        add(
            item.get("transitioned_at"),
            "STRATEGY",
            item.get("strategy_date"),
            f"{item.get('from')} → {item.get('to')}",
            item.get("reason"),
        )

    candidate = _mapping(view.get("candidate"))
    for item in map(_mapping, _sequence(candidate.get("history"))):
        add(
            item.get("scanned_at"),
            "SCREEN",
            item.get("strategy_date"),
            item.get("symbol"),
            item.get("result"),
            ", ".join(str(value) for value in _sequence(item.get("failed_gates")))
            or "all deterministic gates passed",
        )

    agent = _mapping(view.get("agent"))
    for item in map(
        _mapping,
        [*_sequence(agent.get("reviews")), *_sequence(agent.get("advisories"))],
    ):
        add(
            item.get("ended_at") or item.get("started_at"),
            "QWEN",
            item.get("strategy_date"),
            f"{item.get('kind')} · {item.get('symbol')}",
            item.get("decision") or item.get("status"),
            item.get("summary") or item.get("reason"),
        )

    mcp = _mapping(view.get("mcp"))
    for item in map(_mapping, _sequence(mcp.get("timeline"))):
        add(
            item.get("called_at"),
            "ALPACA MCP",
            None,
            item.get("tool"),
            item.get("status"),
            f"{item.get('principal') or 'unknown'} · {item.get('duration_ms') or 0} ms",
        )

    orders = _mapping(view.get("orders"))
    for item in map(_mapping, _sequence(orders.get("timeline"))):
        add(
            item.get("observed_at"),
            "ORDER",
            item.get("strategy_date"),
            item.get("purpose"),
            item.get("event") or item.get("broker_status"),
            f"{item.get('from') or 'none'} → {item.get('to') or 'none'}",
        )
    for item in map(_mapping, _sequence(orders.get("fills"))):
        add(
            item.get("filled_at"),
            "FILL",
            item.get("strategy_date"),
            item.get("symbol"),
            f"{item.get('side')} {item.get('quantity')}",
            _money(item.get("price")),
        )

    safety = _mapping(view.get("safety"))
    for item in map(_mapping, _sequence(safety.get("entry_permission_history"))):
        add(
            item.get("consumed_at") or item.get("revoked_at") or item.get("armed_at"),
            "ENTRY PERMIT",
            item.get("strategy_date"),
            "date-bound authorization",
            item.get("state"),
            f"expires {item.get('expires_at') or 'unknown'}",
        )
    kill = _mapping(safety.get("kill_switch"))
    for item in map(_mapping, _sequence(kill.get("history"))):
        add(
            item.get("created_at"),
            "KILL SWITCH",
            None,
            "operator safety control",
            "ON" if item.get("enabled") else "OFF",
            item.get("reason"),
        )

    rows.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
    return rows


def main() -> None:
    st.set_page_config(
        page_title="ThetaTrap | Public Audit",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(
        """
        <style>
        :root {
          --tt-ink: #12211c;
          --tt-muted: #5d6f67;
          --tt-line: #dce7e1;
          --tt-green: #0f7654;
          --tt-mint: #dff7ec;
          --tt-amber: #f4b544;
          --tt-surface: #f7faf8;
          --tt-card: rgba(255,255,255,.82);
        }
        @media (prefers-color-scheme: dark) {
          :root {
            --tt-ink: #edf8f2;
            --tt-muted: #a9bbb2;
            --tt-line: #34473e;
            --tt-green: #72d6aa;
            --tt-surface: #121c18;
            --tt-card: #17241e;
          }
        }
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        #MainMenu,
        footer { visibility: hidden; height: 0; }
        .block-container { max-width: 1480px; padding-top: 1.6rem; padding-bottom: 4rem; }
        .tt-hero {
          background: linear-gradient(122deg, #071a14 0%, #103b2e 58%, #176348 100%);
          border: 1px solid rgba(255,255,255,.12);
          border-radius: 20px;
          padding: 1.45rem 1.6rem 1.35rem;
          color: #f5fff9;
          box-shadow: 0 20px 45px rgba(10,48,35,.16);
          margin-bottom: 1.15rem;
        }
        .tt-kicker { color: #93e3bd; font-size: .72rem; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
        .tt-hero h1 { margin: .25rem 0 .3rem; color: #ffffff; font-size: clamp(2rem, 4vw, 3.3rem); letter-spacing: -.045em; }
        .tt-hero p { margin: 0; color: #cde4d8; max-width: 850px; font-size: 1rem; }
        .tt-badges { display: flex; gap: .45rem; flex-wrap: wrap; margin-top: 1rem; }
        .tt-badge { border: 1px solid rgba(255,255,255,.22); border-radius: 999px; padding: .28rem .62rem; font-size: .7rem; font-weight: 750; letter-spacing: .04em; }
        .tt-section { margin-top: 1.8rem; margin-bottom: .55rem; }
        .tt-section small { color: var(--tt-green); font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
        .tt-section h2 { margin: .18rem 0 .2rem; color: var(--tt-ink); font-size: 1.5rem; letter-spacing: -.025em; }
        .tt-section p { margin: 0; color: var(--tt-muted); max-width: 920px; }
        .tt-live {
          border-left: 5px solid var(--tt-green);
          background: var(--tt-mint);
          color: #0d4d39;
          border-radius: 12px;
          padding: .75rem .95rem;
          font-weight: 750;
          margin: .25rem 0 .85rem;
        }
        .tt-live.armed { border-left-color: #c47a00; background: #fff1d3; color: #754600; }
        .tt-profile {
          display: inline-block;
          border: 1px solid #f0c36a;
          background: #fff7e6;
          color: #754b00;
          border-radius: 999px;
          padding: .25rem .62rem;
          font-size: .72rem;
          font-weight: 800;
          letter-spacing: .035em;
          margin: .2rem 0 .65rem;
        }
        .tt-pivot {
          border: 1px solid #c9d9ff;
          border-left: 5px solid #3568d4;
          background: #f3f7ff;
          color: #17386f;
          border-radius: 12px;
          padding: .85rem 1rem;
          margin: .65rem 0 1rem;
        }
        .tt-pivot strong { color: #17386f; }
        .tt-role {
          border: 1px solid var(--tt-line);
          background: var(--tt-card);
          border-radius: 12px;
          padding: .8rem .9rem;
          min-height: 132px;
        }
        .tt-role h4 { margin: 0 0 .35rem; color: var(--tt-ink); }
        .tt-role p { margin: 0; color: var(--tt-muted); font-size: .9rem; }
        [data-testid="stMetric"] {
          border: 1px solid var(--tt-line);
          background: var(--tt-card);
          border-radius: 12px;
          padding: .75rem .8rem;
          min-height: 94px;
        }
        [data-testid="stMetricLabel"] { color: var(--tt-muted); }
        [data-testid="stMetricValue"] { color: var(--tt-ink); }
        [data-testid="stMetricValue"] p {
          color: var(--tt-ink);
          font-size: clamp(1.25rem, 1.8vw, 1.85rem) !important;
          line-height: 1.08;
          white-space: nowrap;
        }
        [data-testid="stDataFrame"] { border: 1px solid var(--tt-line); border-radius: 12px; overflow: hidden; }
        div[data-testid="stExpander"] { border-color: var(--tt-line); border-radius: 12px; }
        @media (max-width: 640px) {
          .block-container { padding: .9rem .75rem 3rem; }
          .tt-hero { padding: 1.1rem; border-radius: 16px; }
          .tt-hero h1 { font-size: 2.1rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    build = display_build_sha()
    st.markdown(
        f"""
        <section class="tt-hero">
          <div class="tt-kicker">Live competition audit · build {build}</div>
          <h1>ThetaTrap</h1>
          <p>A bounded MCP-native options agent. The original earnings strategy stayed fail-closed when Alpaca Basic data could not support a valid four-leg trade; the date-scoped Sep 3 Intraday Theta Canary is the transparent, testable response.</p>
          <div class="tt-badges">
            <span class="tt-badge">PAPER TRADING</span>
            <span class="tt-badge">ALPACA OFFICIAL MCP</span>
            <span class="tt-badge">PUBLIC READ-ONLY VIEW</span>
            <span class="tt-badge">BASIC INDICATIVE DATA</span>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )

    _render_live_report()


@st.fragment(run_every="30s")
def _render_live_report() -> None:
    """Refresh evidence without creating any public mutation surface."""

    database_path = Path(
        os.environ.get("THETATRAP_DATABASE_PATH", "/data/thetatrap.sqlite3")
    )
    try:
        report = build_operational_report(
            database_path,
            execution_enabled=_env_bool("THETATRAP_EXECUTION_ENABLED", False),
            read_only=_env_bool("THETATRAP_READ_ONLY", True),
            environment=os.environ.get("THETATRAP_ENVIRONMENT"),
        )
    except ReportUnavailable as exc:
        st.warning(f"Evidence is not available yet: {exc}")
        st.info("The public viewer remains online while the worker is investigated.")
        st.stop()

    view = build_public_view(report)
    _render_result_snapshot(view)
    _render_problem_and_strategy(view)
    _render_ai_mcp_proof(view)
    _render_verified_results(view)
    _render_safety_and_robustness(view)
    _render_limitations_and_evidence(view)


def _render_result_snapshot(view: Mapping[str, Any]) -> None:
    mode = _mapping(view.get("mode"))
    health = _mapping(view.get("health"))
    mcp = _mapping(view.get("mcp"))
    strategy = _mapping(view.get("strategy"))
    portfolio = _mapping(view.get("portfolio"))
    primary_review = _primary_execution_review(view)

    _section_heading(
        "00 · RESULT SNAPSHOT",
        "The outcome, before the implementation details",
        "Current broker and agent evidence from the competition account. Values refresh every 30 seconds.",
    )
    banner = escape(str(mode.get("banner") or "PAPER · TRADING DISARMED"))
    armed = mode.get("trading_state") == "ARMED"
    st.markdown(
        f'<div class="tt-live{" armed" if armed else ""}">{banner}</div>',
        unsafe_allow_html=True,
    )
    columns = st.columns(6)
    columns[0].metric("Final run state", strategy.get("state", "NOT_STARTED"))
    position_label = str(portfolio.get("position", "UNKNOWN"))
    if position_label == "OPEN" and portfolio.get("position_count"):
        position_label += f" · {portfolio['position_count']} legs"
    columns[1].metric("Position", position_label)
    columns[2].metric("Starting equity", _money(portfolio.get("first_equity")))
    columns[3].metric("Latest equity", _money(portfolio.get("latest_equity")))
    columns[4].metric(
        "Observed P&L", _money(portfolio.get("observed_change"), signed=True)
    )
    columns[5].metric("Qwen result", _review_display_outcome(primary_review))

    market = (
        "OPEN"
        if health.get("market_is_open") is True
        else "CLOSED"
        if health.get("market_is_open") is False
        else "UNKNOWN"
    )
    heartbeat_age = health.get("heartbeat_age_seconds")
    freshness = (
        f"{int(heartbeat_age)}s old"
        if isinstance(heartbeat_age, (int, float))
        else "unavailable"
    )
    st.caption(
        f"Worker {str(health.get('status') or 'unknown').upper()} · "
        f"MCP {str(mcp.get('status') or 'unknown').upper()} · market {market} · "
        f"heartbeat {health.get('observed_at') or 'not observed'} ({freshness}) · "
        f"last successful MCP call {mcp.get('last_successful_call_at') or 'not observed'}"
    )
    if health.get("stale"):
        st.warning(
            "Worker evidence is stale or missing; do not treat the displayed state as current."
        )


def _render_problem_and_strategy(view: Mapping[str, Any]) -> None:
    strategy = _mapping(view.get("strategy"))
    profile = _mapping(strategy.get("profile"))
    _section_heading(
        "01 · PROBLEM & STRATEGY",
        "Sell bounded premium only when the evidence is usable",
        "ThetaTrap started with earnings volatility, then introduced one transparent Sep 3 canary after the Basic feed repeatedly failed complete four-leg gates.",
    )
    cards = st.columns(3)
    cards[0].markdown(
        '<div class="tt-role"><h4>Problem</h4><p>Options premium can be rich around '
        "events, but weak quotes, timing mistakes, and unbounded execution risk can "
        "erase the thesis.</p></div>",
        unsafe_allow_html=True,
    )
    cards[1].markdown(
        '<div class="tt-role"><h4>Original strategy</h4><p>A one-contract, defined-risk '
        "earnings iron condor proceeds only after event, quote, liquidity, and loss "
        "gates all pass.</p></div>",
        unsafe_allow_html=True,
    )
    cards[2].markdown(
        '<div class="tt-role"><h4>Sep 3 adaptation</h4><p>The date-scoped QQQ/SPY '
        "Intraday Theta Canary uses a liquid universe and a same-day exit while "
        "retaining deterministic construction and risk.</p></div>",
        unsafe_allow_html=True,
    )
    if profile.get("profile_id") == CANARY_PROFILE_ID:
        st.markdown(
            '<div class="tt-pivot"><strong>Active profile: Intraday Theta Canary</strong><br>'
            "Competition-only adaptation—not an automatic fallback and not a claim "
            "of proven profitability.</div>",
            unsafe_allow_html=True,
        )
    st.dataframe(
        [
            {"control": "Universe", "frozen value": profile.get("universe")},
            {"control": "Expiration", "frozen value": profile.get("expiration")},
            {"control": "Entry window", "frozen value": profile.get("entry_window")},
            {
                "control": "Cancel / exit",
                "frozen value": (
                    f"cancel {profile.get('cancel_at') or 'n/a'} · exit starts "
                    f"{profile.get('exit_start') or 'n/a'} · flat target "
                    f"{profile.get('flat_target') or 'n/a'}"
                ),
            },
            {"control": "Structure", "frozen value": profile.get("structure")},
            {
                "control": "Risk",
                "frozen value": profile.get("maximum_defined_loss"),
            },
        ],
        width="stretch",
        hide_index=True,
    )


def _render_ai_mcp_proof(view: Mapping[str, Any]) -> None:
    agent = _mapping(view.get("agent"))
    mcp = _mapping(view.get("mcp"))
    primary = _primary_execution_review(view)
    reviews = [_mapping(item) for item in _sequence(agent.get("reviews"))]
    advisories = [_mapping(item) for item in _sequence(agent.get("advisories"))]
    _section_heading(
        "02 · AI + ALPACA MCP PROOF",
        "Qwen reasoned over bounded, auditable tools",
        "The model reviewed an immutable deterministic candidate. It could inspect evidence and allow or veto, but it could not change the symbol, legs, quantity, price, or risk limit.",
    )
    st.dataframe(
        [
            {
                "component": "Qwen via Featherless",
                "responsibility": "Read Alpaca MCP evidence; explain; allow or veto the frozen candidate",
                "cannot do": "Change strikes, size, order price, risk budget, exits, or failed gates",
            },
            {
                "component": "Deterministic Python",
                "responsibility": "Construct, gate, authorize, submit, reconcile, cancel, and exit",
                "cannot do": "Invent missing data or exceed the frozen profile",
            },
        ],
        width="stretch",
        hide_index=True,
    )
    if primary:
        trace = [_mapping(item) for item in _sequence(primary.get("tool_trace"))]
        official_mcp_count = sum(
            1 for item in trace if item.get("kind") == "official MCP"
        )
        columns = st.columns(5)
        columns[0].metric("Candidate", primary.get("symbol") or "unknown")
        columns[1].metric("Model", _model_label(primary.get("model")))
        columns[2].metric("Run status", primary.get("status") or "unknown")
        columns[3].metric("Decision", _review_display_outcome(primary))
        columns[4].metric("Official MCP", f"{official_mcp_count} of {len(trace)} calls")
        st.success(
            f"Primary execution review: {primary.get('strategy_date') or 'unknown date'} "
            f"{primary.get('symbol') or 'candidate'} · "
            f"{_review_display_outcome(primary)}. Qwen's final call was the exact "
            "`place_option_order`; the deterministic gateway rechecked policy before "
            "broker submission."
        )
        if trace:
            st.markdown("#### Successful Qwen → Alpaca MCP tool sequence")
            st.dataframe(
                [
                    {
                        "step": step,
                        "source": item.get("kind"),
                        "tool": item.get("tool"),
                        "result": item.get("status"),
                    }
                    for step, item in enumerate(trace, start=1)
                ],
                width="stretch",
                hide_index=True,
            )
    else:
        st.info("No completed Qwen execution review has been persisted yet.")

    policy_blocks = [item for item in reviews if _is_policy_gateway_block(item)]
    if policy_blocks:
        st.info(
            "Safety gateway evidence: the SPY Qwen attempt raised PolicyError because "
            "its required read sequence was incomplete. The gateway blocked it before "
            "broker mutation; the worker then evaluated the next ranked candidate."
        )
        st.dataframe(
            [
                {
                    "date": item.get("strategy_date"),
                    "symbol": item.get("symbol"),
                    "outcome": "SAFETY GATEWAY BLOCKED",
                    "reason": item.get("reason"),
                    "ended_at": item.get("ended_at"),
                }
                for item in policy_blocks
            ],
            width="stretch",
            hide_index=True,
        )

    with st.expander("All Qwen decisions and read-only advisories", expanded=False):
        if reviews:
            st.caption("Execution reviews")
            st.dataframe(
                [
                    {
                        "date": item.get("strategy_date"),
                        "symbol": item.get("symbol"),
                        "rank": item.get("rank"),
                        "status": item.get("status"),
                        "outcome": _review_display_outcome(item),
                        "reason": item.get("reason"),
                        "model": item.get("model"),
                        "tools": len(_sequence(item.get("tool_trace"))),
                        "ended_at": item.get("ended_at"),
                    }
                    for item in reviews
                ],
                width="stretch",
                hide_index=True,
            )
        if advisories:
            st.caption(
                "Rejected-candidate advisories are read-only and cannot reverse a deterministic gate."
            )
            st.dataframe(advisories, width="stretch", hide_index=True)
    with st.expander("Recent bounded agent and Alpaca MCP history", expanded=False):
        timeline = _sequence(agent.get("timeline"))
        if timeline:
            st.caption("Qwen tool history")
            st.dataframe(timeline, width="stretch", hide_index=True)
        mcp_timeline = _sequence(mcp.get("timeline"))
        if mcp_timeline:
            st.caption(f"Recent official Alpaca MCP calls · {len(mcp_timeline)} rows")
            st.dataframe(mcp_timeline, width="stretch", hide_index=True)
        if not timeline and not mcp_timeline:
            st.caption("No MCP tool history has been published yet.")


def _render_verified_results(view: Mapping[str, Any]) -> None:
    orders = _mapping(view.get("orders"))
    portfolio = _mapping(view.get("portfolio"))
    strategy = _mapping(view.get("strategy"))
    candidate = _primary_execution_candidate(view)
    chains = [_mapping(item) for item in _sequence(orders.get("chains"))]
    fills = [_mapping(item) for item in _sequence(orders.get("fills"))]
    broker_leg_fills = [
        _mapping(item) for item in _sequence(orders.get("broker_leg_fills"))
    ]
    filled_entry = next(
        (
            chain
            for chain in chains
            if chain.get("purpose") == "entry"
            and chain.get("broker_fill_confirmed") is True
        ),
        {},
    )
    filled_exit = next(
        (
            chain
            for chain in chains
            if chain.get("purpose") == "exit"
            and chain.get("broker_fill_confirmed") is True
        ),
        {},
    )
    _section_heading(
        "03 · VERIFIED RESULTS",
        "Separate broker facts from model claims",
        "Order status, nested leg fills, positions, and account equity come from persisted Alpaca observations. Account equity is the authoritative result.",
    )
    columns = st.columns(6)
    columns[0].metric("Trade", candidate.get("symbol") or "none")
    columns[1].metric(
        "Entry fill", _mleg_price_label(filled_entry.get("broker_fill_price"))
    )
    columns[2].metric(
        "Exit fill", _mleg_price_label(filled_exit.get("broker_fill_price"))
    )
    columns[3].metric(
        "Gross spread P&L",
        _money(orders.get("broker_mleg_cash_flow_ex_fees"), signed=True),
    )
    columns[4].metric("End state", portfolio.get("position", "UNKNOWN"))
    columns[5].metric(
        "Account P&L", _money(portfolio.get("observed_change"), signed=True)
    )

    if orders.get("broker_round_trip_confirmed"):
        message = (
            "Broker-confirmed round trip: "
            f"{_markdown_currency(_mleg_price_label(filled_entry.get('broker_fill_price')))} in, "
            f"{_markdown_currency(_mleg_price_label(filled_exit.get('broker_fill_price')))} out, and "
            f"{_markdown_currency(_money(orders.get('broker_mleg_cash_flow_ex_fees'), signed=True))} "
            "gross spread P&L from the reported MLEG prices. The account ended flat."
        )
        difference = _difference(
            portfolio.get("observed_change"),
            orders.get("broker_mleg_cash_flow_ex_fees"),
        )
        if difference not in (None, 0.0):
            message += (
                " Account equity differs by "
                f"{_markdown_currency(_money(difference, signed=True))}; "
                "the dashboard leaves that difference unattributed."
            )
        st.success(message)

    if chains:
        st.markdown("#### Entry / exit execution evidence")
        st.dataframe(
            [
                {
                    "symbol": chain.get("symbol"),
                    "purpose": chain.get("purpose"),
                    "state": chain.get("state"),
                    "initial limit": _mleg_price_label(
                        chain.get("initial_limit_price")
                    ),
                    "final limit": _mleg_price_label(chain.get("final_limit_price")),
                    "submitted (ET)": _et_timestamp(chain.get("submitted_at")),
                    "filled (ET)": _et_timestamp(chain.get("broker_filled_at")),
                    "broker MLEG fill": _mleg_price_label(
                        chain.get("broker_fill_price")
                    ),
                    "broker_fill_confirmed": chain.get("broker_fill_confirmed"),
                    "attempts": chain.get("attempt_count"),
                }
                for chain in chains
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No paper order lifecycle has been published yet.")

    legs = _sequence(candidate.get("legs"))
    if candidate and legs:
        st.markdown(
            f"#### Selected four-leg structure · {candidate.get('symbol') or 'candidate'}"
        )
        st.caption(
            "Deterministic quote snapshot "
            f"{_et_timestamp(candidate.get('scanned_at'))} · "
            "proposed credit "
            f"{_markdown_currency(_money(candidate.get('proposed_credit')))} · "
            "maximum loss "
            f"{_markdown_currency(_money(candidate.get('maximum_loss')))}. "
            "These are selection-time quotes, not reconstructed fills."
        )
        st.dataframe(legs, width="stretch", hide_index=True)

    if broker_leg_fills:
        st.markdown(
            f"#### Broker-reported leg fills · {len(broker_leg_fills)} observations"
        )
        st.dataframe(
            [
                {
                    **{key: item.get(key) for key in ("purpose", "symbol", "side")},
                    "intent": item.get("position_intent"),
                    "quantity": item.get("quantity"),
                    "price": _money(item.get("price")),
                    "filled (ET)": _et_timestamp(item.get("filled_at")),
                }
                for item in broker_leg_fills
            ],
            width="stretch",
            hide_index=True,
        )
        if not fills:
            st.caption(
                "These values come directly from Alpaca's nested MLEG order observations. "
                "The legacy normalized fill table has no rows, so it is not used for P&L."
            )
    elif fills:
        st.markdown("#### Normalized broker leg fill prices")
        st.dataframe(fills, width="stretch", hide_index=True)

    positions = _sequence(portfolio.get("positions"))
    if positions:
        st.markdown("#### Current broker-observed option positions")
        st.dataframe(positions, width="stretch", hide_index=True)

    observed_rows = [
        item
        for item in map(_mapping, _sequence(view.get("competition_days")))
        if item.get("scans") or item.get("order_chains")
    ]
    if observed_rows:
        st.markdown("#### Competition-day record")
        st.dataframe(observed_rows, width="stretch", hide_index=True)

    equity_history = [
        item
        for item in map(_mapping, _sequence(portfolio.get("equity_history")))
        if item.get("equity") is not None
    ]
    st.caption(
        "First observed equity "
        f"{_markdown_currency(_money(portfolio.get('first_equity')))} · latest "
        f"{_markdown_currency(_money(portfolio.get('latest_equity')))} at "
        f"{portfolio.get('observed_at') or 'not observed'} · strategy state "
        f"{strategy.get('state') or 'unknown'}."
    )

    _render_collapsed_evidence(view, equity_history=equity_history)


def _render_collapsed_evidence(
    view: Mapping[str, Any], *, equity_history: Sequence[Mapping[str, Any]]
) -> None:
    candidate = _mapping(view.get("candidate"))
    strategy = _mapping(view.get("strategy"))
    orders = _mapping(view.get("orders"))
    portfolio = _mapping(view.get("portfolio"))
    history = [_mapping(item) for item in _sequence(candidate.get("history"))]
    with st.expander(
        "Complete scans, deterministic gates, and candidate legs", expanded=False
    ):
        matrix = _sequence(candidate.get("scan_matrix"))
        if matrix:
            st.caption("All-symbol scan matrix")
            st.dataframe(matrix, width="stretch", hide_index=True)
        if history:
            st.caption("Every persisted candidate evaluation")
            st.dataframe(history, width="stretch", hide_index=True)
        if not matrix and not history:
            st.caption("No candidate evidence has been published yet.")

    with st.expander(
        "Full activity, state, order, and equity histories", expanded=False
    ):
        activity = _sequence(view.get("activity_timeline"))
        if activity:
            counts = Counter(
                str(_mapping(item).get("category") or "UNKNOWN") for item in activity
            )
            st.caption(
                "Activity · "
                + " · ".join(
                    f"{category}: {count:,}"
                    for category, count in sorted(counts.items())
                )
            )
            st.dataframe(activity, width="stretch", hide_index=True, height=430)
        if _sequence(strategy.get("transitions")):
            st.caption("Strategy state history")
            st.dataframe(
                _sequence(strategy.get("transitions")), width="stretch", hide_index=True
            )
        if _sequence(orders.get("timeline")):
            st.caption("Order-status history")
            st.dataframe(
                _sequence(orders.get("timeline")), width="stretch", hide_index=True
            )
        if equity_history:
            st.caption("Equity observation history")
            st.dataframe(equity_history, width="stretch", hide_index=True)
        if _sequence(portfolio.get("position_history")):
            st.caption("Position observation history")
            st.dataframe(
                _sequence(portfolio.get("position_history")),
                width="stretch",
                hide_index=True,
            )


def _render_safety_and_robustness(view: Mapping[str, Any]) -> None:
    safety = _mapping(view.get("safety"))
    kill = _mapping(safety.get("kill_switch"))
    permission = _mapping(safety.get("entry_permission"))
    _section_heading(
        "04 · SAFETY & ROBUSTNESS",
        "The model cannot bypass deterministic controls",
        "One-shot authorization, defined loss, immutable order intent, restart-safe reconciliation, and a private kill switch contain the paper-trading workflow.",
    )
    columns = st.columns(5)
    columns[0].metric(
        "Kill switch",
        "ON" if kill.get("enabled") else "OFF" if kill.get("known") else "UNKNOWN",
    )
    columns[1].metric("Entry permit", permission.get("state", "MISSING"))
    columns[2].metric("Max defined loss", _money(safety.get("maximum_defined_loss")))
    columns[3].metric("Max contracts", safety.get("maximum_contracts", 1))
    columns[4].metric("Equity kill floor", _money(safety.get("equity_kill_threshold")))
    st.info(
        "Public evidence only: this container has a read-only database mount, no "
        "broker or model credentials, no order controls, and no mutation callbacks."
    )
    with st.expander("Authorization and kill-switch histories", expanded=False):
        permissions = _sequence(safety.get("entry_permission_history"))
        if permissions:
            st.caption("Date-bound entry permissions")
            st.dataframe(permissions, width="stretch", hide_index=True)
        history = _sequence(kill.get("history"))
        if history:
            st.caption("Kill-switch audit")
            st.dataframe(history, width="stretch", hide_index=True)
        if not permissions and not history:
            st.caption("No safety-control history has been published yet.")


def _render_limitations_and_evidence(view: Mapping[str, Any]) -> None:
    _section_heading(
        "05 · LIMITATIONS & EVIDENCE",
        "An execution demonstration, not proof of alpha",
        "The full sanitized public evidence remains downloadable for independent review.",
    )
    st.warning("PAPER TRADING · BASIC INDICATIVE OPTIONS DATA · SIMULATED FILLS")
    limitations = [
        "Alpaca Basic uses IEX stock data and indicative, non-OPRA option quotes.",
        "Paper fills are simulated and do not establish live execution quality or profitability.",
        "Total account equity is authoritative; gross MLEG price arithmetic can differ from account P&L.",
        "The Sep 3 canary is a competition-only adaptation, not a backtested or generic fallback strategy.",
        "This one paper result does not prove repeatable alpha and is not investment advice.",
    ]
    for limitation in limitations:
        st.markdown(f"- {limitation}")
    with st.expander("Audit digest and evidence notes", expanded=False):
        st.code(str(view.get("report_digest") or "unavailable"), language=None)
        st.caption(
            "The MCP timeline is intentionally labeled recent history because the "
            "operational report caps retained display rows."
        )
    st.download_button(
        "Download sanitized public evidence (JSON)",
        data=serialize_public_evidence(view),
        file_name="thetatrap-public-evidence.json",
        mime="application/json",
    )


def serialize_public_evidence(view: Mapping[str, Any]) -> str:
    """Serialize only the already-public projection, with defense-in-depth redaction."""

    sanitized = redact_public_identifiers(dict(view))
    return json.dumps(sanitized, indent=2, sort_keys=True, ensure_ascii=True)


def _primary_execution_review(view: Mapping[str, Any]) -> Mapping[str, Any]:
    reviews = [
        _mapping(item) for item in _sequence(_mapping(view.get("agent")).get("reviews"))
    ]
    return _select_primary_execution_review(reviews)


def _select_primary_execution_review(
    reviews: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    if not reviews:
        return {}

    def priority(item: Mapping[str, Any]) -> tuple[int, int, str]:
        completed_allow = (
            str(item.get("status") or "").upper() == "COMPLETED"
            and str(item.get("decision") or "").upper() == "ALLOW"
        )
        sep3_qqq = (
            completed_allow
            and item.get("strategy_date") == CANARY_STRATEGY_DATE
            and str(item.get("symbol") or "").upper() == "QQQ"
        )
        return (int(sep3_qqq), int(completed_allow), str(item.get("ended_at") or ""))

    return max(reviews, key=priority)


def _primary_execution_candidate(view: Mapping[str, Any]) -> Mapping[str, Any]:
    primary = _primary_execution_review(view)
    history = [
        _mapping(item)
        for item in _sequence(_mapping(view.get("candidate")).get("history"))
    ]
    matches = [
        item
        for item in history
        if item.get("strategy_date") == primary.get("strategy_date")
        and item.get("symbol") == primary.get("symbol")
        and item.get("eligible") is True
    ]
    if matches:
        return max(matches, key=lambda item: str(item.get("scanned_at") or ""))
    eligible = [item for item in history if item.get("eligible") is True]
    if eligible:
        return max(eligible, key=lambda item: str(item.get("scanned_at") or ""))
    return {}


def _is_policy_gateway_block(review: Mapping[str, Any]) -> bool:
    return str(review.get("reason") or "").lower() == "policyerror"


def _review_display_outcome(review: Mapping[str, Any]) -> str:
    if not review:
        return "not run"
    if _is_policy_gateway_block(review):
        return "SAFETY GATEWAY BLOCKED"
    return str(review.get("decision") or review.get("status") or "unknown").upper()


def _model_label(value: Any) -> str:
    primary = str(value or "unknown").split("|", 1)[0]
    return primary.removeprefix("Qwen/")


def _option_underlying(value: Any) -> str | None:
    match = OPTION_SYMBOL_PATTERN.fullmatch(str(value or "").upper())
    return match.group(1) if match else None


def _broker_mleg_cash_flow(chains: Sequence[Mapping[str, Any]]) -> str | None:
    """Calculate cash flow only from complete broker-reported MLEG fills."""

    filled = [
        _mapping(chain)
        for chain in chains
        if _mapping(chain).get("broker_fill_confirmed") is True
    ]
    if not filled:
        return None
    cash_flow = 0.0
    for chain in filled:
        price = _number(chain.get("broker_fill_price"))
        quantity = _number(chain.get("broker_filled_quantity"))
        if price is None or quantity is None:
            return None
        cash_flow -= price * quantity * 100
    return f"{cash_flow:.2f}"


def _mleg_price_label(value: Any) -> str:
    price = _number(value)
    if price is None:
        return "not filled"
    direction = "credit" if price < 0 else "debit"
    return f"${abs(price):,.2f} {direction}"


def _markdown_currency(value: str) -> str:
    return value.replace("$", r"\$")


def _difference(left: Any, right: Any) -> float | None:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return None
    return round(left_number - right_number, 2)


def _et_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "not submitted"
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    eastern = timestamp.astimezone(ZoneInfo("America/New_York"))
    return f"{eastern:%b} {eastern.day}, {eastern:%H:%M:%S} ET"


def _section_heading(kicker: str, title: str, description: str) -> None:
    st.markdown(
        '<div class="tt-section">'
        f"<small>{escape(kicker)}</small>"
        f"<h2>{escape(title)}</h2>"
        f"<p>{escape(description)}</p>"
        "</div>",
        unsafe_allow_html=True,
    )


def _is_private_identifier_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized == "id":
        return True
    if not (normalized.endswith("_id") or normalized.endswith("_ids")):
        return False
    return any(family in normalized for family in IDENTIFIER_FAMILIES)


def _heartbeat_age_seconds(
    observed_at: Any,
    *,
    now: datetime | None = None,
) -> float | None:
    if not isinstance(observed_at, str) or not observed_at.strip():
        return None
    try:
        observed = datetime.fromisoformat(observed_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(
        0.0, (current.astimezone(UTC) - observed.astimezone(UTC)).total_seconds()
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _money(value: Any, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(str(value))
    except ValueError:
        return "n/a"
    if number < 0:
        return f"-${abs(number):,.2f}"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}${number:,.2f}"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


if __name__ == "__main__":
    main()
