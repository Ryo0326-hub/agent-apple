"""Public, read-only evidence dashboard with no operator mutation imports."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

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
    equity = _mapping(portfolio.get("equity"))
    kill = _mapping(report.get("kill_switch"))
    one_shot = _mapping(report.get("one_shot_entry"))
    safety = _mapping(report.get("safety"))
    data_profile = _mapping(report.get("data_profile"))
    heartbeat_age = _heartbeat_age_seconds(health.get("observed_at"))

    chains: list[dict[str, Any]] = []
    order_timeline: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    for chain_value in _sequence(orders.get("chains")):
        chain = _mapping(chain_value)
        attempts = _sequence(chain.get("attempts"))
        chain_fills = _sequence(chain.get("fills"))
        status_history = _sequence(chain.get("status_history"))
        chains.append(
            {
                "purpose": chain.get("purpose"),
                "state": chain.get("state"),
                "created_at": chain.get("created_at"),
                "updated_at": chain.get("updated_at"),
                "attempt_count": len(attempts),
                "fill_count": len(chain_fills),
            }
        )
        for status_value in status_history:
            status = _mapping(status_value)
            order_timeline.append(
                {
                    "observed_at": status.get("observed_at"),
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
    reviews = [
        _public_review(_mapping(item), kind="QWEN_DECISION")
        for item in _sequence(agent.get("reviews"))
    ]
    advisories = [
        _public_review(_mapping(item), kind="READ_ONLY_ADVISORY")
        for item in _sequence(agent.get("advisories"))
    ]
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
            "stale": (
                heartbeat_age is None
                or heartbeat_age > HEARTBEAT_STALE_SECONDS
            ),
        },
        "mcp": {
            "status": mcp.get("operational_status") or session.get("status"),
            "package_version": session.get("package_version"),
            "tool_count": session.get("tool_count"),
            "required_schema_hash": session.get("required_schema_hash"),
            "last_successful_call_at": _mapping(
                mcp.get("last_successful_call")
            ).get("called_at"),
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
            "no_trade_reason": strategy.get("no_trade_reason"),
            "transitions": transitions,
            "run_count": len(_sequence(strategy.get("run_history"))),
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
            "model": latest_agent.get("model"),
            "status": latest_agent.get("status"),
            "decision": (
                _mapping(latest_agent.get("result")).get("decision")
                or _mapping(latest_agent.get("result")).get("outcome")
                or latest_agent.get("veto_reason")
            ),
            "veto_reason": latest_agent.get("veto_reason"),
            "error_type": latest_agent.get("error_type"),
            "tool_trace": trace,
            "reviews": reviews,
            "advisories": advisories,
            "timeline": agent_timeline,
        },
        "orders": {
            "chain_count": orders.get("chain_count", 0),
            "fill_count": orders.get("fill_count", 0),
            "option_cash_flow_ex_fees": orders.get(
                "option_cash_flow_ex_fees", "0.00"
            ),
            "chains": chains,
            "timeline": sorted(
                order_timeline, key=lambda item: str(item.get("observed_at") or "")
            ),
            "fills": sorted(
                fills, key=lambda item: str(item.get("filled_at") or "")
            ),
        },
        "portfolio": {
            "position": (
                "FLAT"
                if position.get("is_flat") is True
                else "OPEN"
                if position
                else "UNKNOWN"
            ),
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
                for item in map(
                    _mapping, _sequence(portfolio.get("position_history"))
                )
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
                for item in map(
                    _mapping, _sequence(safety.get("entry_permissions"))
                )
            ],
            "maximum_defined_loss": safety.get("maximum_defined_loss", "500.00"),
            "maximum_contracts": safety.get("maximum_contracts", 1),
            "equity_kill_threshold": safety.get(
                "equity_kill_threshold", "99000.00"
            ),
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
          <p>A bounded MCP-native AI agent that screens earnings options, asks Qwen for a qualitative risk decision, and lets deterministic policy own every number and broker boundary.</p>
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
    _render_status(view)
    _render_strategy(view)
    _render_agent(view)
    _render_orders_and_portfolio(view)
    _render_safety(view)
    _render_limitations(view)


def _render_status(view: Mapping[str, Any]) -> None:
    mode = _mapping(view.get("mode"))
    health = _mapping(view.get("health"))
    mcp = _mapping(view.get("mcp"))
    emergency = _mapping(view.get("emergency"))
    account = _mapping(view.get("account"))
    permission = _mapping(view.get("one_shot_entry"))
    data_profile = _mapping(view.get("data_profile"))

    banner = escape(str(mode.get("banner") or "PAPER · TRADING DISARMED"))
    armed = mode.get("trading_state") == "ARMED"
    detail = (
        "date-bound paper entry permission is active"
        if armed
        else "new paper entries are currently blocked"
    )
    st.markdown(
        f'<div class="tt-live{" armed" if armed else ""}">{banner} · {detail}</div>',
        unsafe_allow_html=True,
    )

    columns = st.columns(6)
    columns[0].metric("Worker", str(health.get("status", "unknown")).upper())
    columns[1].metric(
        "Market",
        "OPEN"
        if health.get("market_is_open") is True
        else "CLOSED"
        if health.get("market_is_open") is False
        else "UNKNOWN",
    )
    columns[2].metric("MCP", str(mcp.get("status", "unknown")).upper())
    columns[3].metric("Account", account.get("suffix", "unverified"))
    columns[4].metric("Entry permit", permission.get("state", "MISSING"))
    columns[5].metric(
        "Kill switch",
        "ON"
        if emergency.get("enabled")
        else "OFF"
        if emergency.get("known")
        else "UNKNOWN",
    )
    st.caption(
        f"{str(mode.get('environment', 'unknown')).upper()} · "
        f"{str(data_profile.get('provider') or 'Alpaca').upper()} "
        f"{str(data_profile.get('plan') or 'Basic').upper()} · "
        f"stock {data_profile.get('stock_feed') or 'IEX'} · "
        f"options {data_profile.get('option_feed') or 'indicative'} · "
        f"heartbeat {health.get('observed_at') or 'not observed'}"
    )
    st.markdown(
        '<span class="tt-profile">NON-CONSOLIDATED DATA PROFILE · IEX STOCK · '
        "INDICATIVE OPTIONS</span>",
        unsafe_allow_html=True,
    )
    if health.get("stale"):
        age = health.get("heartbeat_age_seconds")
        detail = (
            f" ({int(age)} seconds old)"
            if isinstance(age, (int, float))
            else ""
        )
        st.warning(
            "Worker evidence is stale or missing"
            + detail
            + "; do not treat the displayed state as current."
        )
    if permission.get("strategy_date"):
        st.caption(f"Active permission date: {permission.get('strategy_date')}")


def _render_strategy(view: Mapping[str, Any]) -> None:
    strategy = _mapping(view.get("strategy"))
    candidate = _mapping(view.get("candidate"))
    _section_heading(
        "01 · Deterministic engine",
        "Every symbol, every scan",
        "The matrix includes configured exclusions and every persisted candidate evaluation—not only the latest or selected fallback.",
    )
    columns = st.columns(5)
    columns[0].metric("Strategy state", strategy.get("state", "NOT_STARTED"))
    columns[1].metric("Strategy date", strategy.get("strategy_date") or "none")
    columns[2].metric("Evaluations", len(_sequence(candidate.get("history"))))
    columns[3].metric(
        "Eligible scans",
        sum(
            1
            for item in _sequence(candidate.get("history"))
            if _mapping(item).get("eligible")
        ),
    )
    columns[4].metric("Run history", strategy.get("run_count", 0))
    if strategy.get("no_trade_reason"):
        st.warning(f"NO_TRADE · {strategy['no_trade_reason']}")

    matrix = _sequence(candidate.get("scan_matrix"))
    st.markdown("#### All-symbol scan matrix")
    if matrix:
        st.dataframe(matrix, width="stretch", hide_index=True)
    else:
        st.info(
            "The frozen event universe is loaded; no symbol scan has been published yet."
        )

    history = _sequence(candidate.get("history"))
    st.markdown("#### Candidate and gate history")
    if not history:
        st.info("No deterministic candidate evaluation has been persisted yet.")
    else:
        st.dataframe(
            [
                {
                    "scanned_at": item.get("scanned_at"),
                    "date": item.get("strategy_date"),
                    "symbol": item.get("symbol"),
                    "result": item.get("result"),
                    "rank": item.get("rank"),
                    "failed_gates": ", ".join(
                        str(value) for value in _sequence(item.get("failed_gates"))
                    ),
                    "iv_ratio": item.get("iv_ratio"),
                    "expected_move": item.get("expected_move"),
                    "credit": item.get("proposed_credit"),
                    "max_loss": item.get("maximum_loss"),
                }
                for item in map(_mapping, history)
            ],
            width="stretch",
            hide_index=True,
        )
        gate_history: list[dict[str, Any]] = []
        for item_value in history:
            item = _mapping(item_value)
            source_rows = _sequence(item.get("gates")) or _sequence(
                item.get("failure_details")
            )
            for gate_value in source_rows:
                gate = _mapping(gate_value)
                gate_history.append(
                    {
                        "scanned_at": item.get("scanned_at"),
                        "date": item.get("strategy_date"),
                        "symbol": item.get("symbol"),
                        "result": item.get("result"),
                        "gate": gate.get("gate"),
                        "passed": gate.get("passed"),
                        "reason": gate.get("reason"),
                        "detail": gate.get("detail"),
                    }
                )
        if gate_history:
            st.caption("Complete per-scan gate ledger")
            st.dataframe(gate_history, width="stretch", hide_index=True)

        detail_limit = 8
        st.caption(
            f"Detailed leg cards show the {min(detail_limit, len(history))} most recent "
            "evaluations; the complete scan and gate ledgers above retain every row."
        )
        for index, item_value in enumerate(history[:detail_limit], start=1):
            item = _mapping(item_value)
            title = (
                f"Scan {index} · {item.get('symbol') or 'unknown'} · "
                f"{item.get('result') or 'UNKNOWN'} · {item.get('scanned_at') or 'time unavailable'}"
            )
            with st.expander(title, expanded=False):
                detail_columns = st.columns(6)
                detail_columns[0].metric("Spot", _money(item.get("spot")))
                detail_columns[1].metric("IV ratio", item.get("iv_ratio") or "n/a")
                detail_columns[2].metric(
                    "Expected move", _money(item.get("expected_move"))
                )
                detail_columns[3].metric(
                    "Credit", _money(item.get("proposed_credit"))
                )
                detail_columns[4].metric("Max loss", _money(item.get("maximum_loss")))
                detail_columns[5].metric("Contracts", item.get("quantity") or "n/a")
                if _sequence(item.get("legs")):
                    st.caption("Immutable four-leg structure")
                    st.dataframe(
                        _sequence(item.get("legs")),
                        width="stretch",
                        hide_index=True,
                    )
                gate_rows = _sequence(item.get("gates")) or _sequence(
                    item.get("failure_details")
                )
                if gate_rows:
                    st.caption("Gate evidence")
                    st.dataframe(gate_rows, width="stretch", hide_index=True)

    transitions = _sequence(strategy.get("transitions"))
    if transitions:
        with st.expander("Strategy state timeline", expanded=False):
            st.dataframe(transitions, width="stretch", hide_index=True)


def _render_agent(view: Mapping[str, Any]) -> None:
    agent = _mapping(view.get("agent"))
    mcp = _mapping(view.get("mcp"))
    _section_heading(
        "02 · AI orchestration",
        "Qwen decisions and bounded tool evidence",
        "Qwen may investigate a candidate and veto it or propose the exact frozen order. It cannot choose strikes, size, credit, or maximum loss.",
    )
    columns = st.columns(4)
    columns[0].metric("Model", agent.get("model") or "not run")
    columns[1].metric("Status", agent.get("status") or "not run")
    columns[2].metric("Decision", agent.get("decision") or "none")
    columns[3].metric("Error", agent.get("error_type") or "none")
    reviews = _sequence(agent.get("reviews"))
    advisories = _sequence(agent.get("advisories"))
    if reviews:
        st.markdown("#### Candidate decisions")
        st.dataframe(
            [
                {
                    "date": item.get("strategy_date"),
                    "symbol": item.get("symbol"),
                    "status": item.get("status"),
                    "decision": item.get("decision"),
                    "reason": item.get("reason"),
                    "model": item.get("model"),
                    "tools": len(_sequence(item.get("tool_trace"))),
                    "ended_at": item.get("ended_at"),
                }
                for item in map(_mapping, reviews)
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No eligible candidate has reached the Qwen decision loop yet.")

    for item_value in reviews:
        item = _mapping(item_value)
        with st.expander(
            f"Qwen trace · {item.get('symbol') or 'unknown'} · {item.get('decision') or item.get('status') or 'unknown'}",
            expanded=False,
        ):
            trace = _sequence(item.get("tool_trace"))
            if trace:
                st.dataframe(trace, width="stretch", hide_index=True)
            else:
                st.caption("No tool call was persisted for this review.")

    if advisories:
        st.markdown("#### Read-only rejected-candidate advisories")
        st.caption(
            "These reviews explain a deterministic rejection. They cannot override a failed gate and expose no broker mutation tool."
        )
        st.dataframe(
            [
                {
                    "date": item.get("strategy_date"),
                    "symbol": item.get("symbol"),
                    "mode": item.get("mode"),
                    "status": item.get("status"),
                    "decision": item.get("decision"),
                    "summary": item.get("summary"),
                    "non_authorizing": item.get("non_authorizing"),
                    "model": item.get("model"),
                    "read_tools": len(_sequence(item.get("tool_trace"))),
                    "ended_at": item.get("ended_at"),
                }
                for item in map(_mapping, advisories)
            ],
            width="stretch",
            hide_index=True,
        )
        for item_value in advisories:
            item = _mapping(item_value)
            with st.expander(
                f"Advisory trace · {item.get('symbol') or 'unknown'} · {item.get('status') or 'unknown'}",
                expanded=False,
            ):
                if item.get("summary"):
                    st.caption(str(item.get("summary")))
                if _sequence(item.get("evidence")):
                    st.dataframe(
                        [
                            {"evidence": evidence}
                            for evidence in _sequence(item.get("evidence"))
                        ],
                        width="stretch",
                        hide_index=True,
                    )
                st.caption(
                    "Non-authorizing: "
                    + ("YES" if item.get("non_authorizing") is True else "UNKNOWN")
                )
                if _sequence(item.get("tool_trace")):
                    st.dataframe(
                        _sequence(item.get("tool_trace")),
                        width="stretch",
                        hide_index=True,
                    )

    timeline = _sequence(agent.get("timeline"))
    if timeline:
        st.markdown("#### Bounded agent-tool timeline")
        st.dataframe(timeline, width="stretch", hide_index=True)
    mcp_timeline = _sequence(mcp.get("timeline"))
    with st.expander(
        f"Official Alpaca MCP call timeline · {len(mcp_timeline)} calls",
        expanded=False,
    ):
        if mcp_timeline:
            st.dataframe(mcp_timeline, width="stretch", hide_index=True)
        else:
            st.caption("No MCP call has been published yet.")


def _render_orders_and_portfolio(view: Mapping[str, Any]) -> None:
    orders = _mapping(view.get("orders"))
    portfolio = _mapping(view.get("portfolio"))
    _section_heading(
        "03 · Broker evidence",
        "Orders, fills, positions, and equity",
        "Broker-reconciled paper evidence is shown separately from model decisions. Account equity is the authoritative competition result.",
    )
    columns = st.columns(6)
    columns[0].metric("Order chains", orders.get("chain_count", 0))
    columns[1].metric("Leg fills", orders.get("fill_count", 0))
    columns[2].metric(
        "Option cash flow, ex-fees",
        _money(orders.get("option_cash_flow_ex_fees")),
    )
    columns[3].metric("Position", portfolio.get("position", "UNKNOWN"))
    columns[4].metric("Latest equity", _money(portfolio.get("latest_equity")))
    columns[5].metric(
        "Observed P&L",
        _money(portfolio.get("observed_change"), signed=True),
    )
    chains = _sequence(orders.get("chains"))
    if chains:
        st.dataframe(chains, width="stretch", hide_index=True)
    else:
        st.info("No paper order chain has been published yet.")
    st.caption(
        f"First observed equity {_money(portfolio.get('first_equity'))} · "
        f"latest observation {portfolio.get('observed_at') or 'not observed'}"
    )

    equity_history = [
        item
        for item in map(_mapping, _sequence(portfolio.get("equity_history")))
        if item.get("equity") is not None
    ]
    if equity_history:
        st.markdown("#### Equity observations")
        st.line_chart(equity_history, x="observed_at", y="equity", height=230)
        with st.expander("Equity observation table", expanded=False):
            st.dataframe(equity_history, width="stretch", hide_index=True)

    fills = _sequence(orders.get("fills"))
    if fills:
        st.markdown("#### Fill ledger")
        st.dataframe(fills, width="stretch", hide_index=True)
    timeline = _sequence(orders.get("timeline"))
    if timeline:
        with st.expander("Order-status timeline", expanded=False):
            st.dataframe(timeline, width="stretch", hide_index=True)


def _render_safety(view: Mapping[str, Any]) -> None:
    safety = _mapping(view.get("safety"))
    kill = _mapping(safety.get("kill_switch"))
    permission = _mapping(safety.get("entry_permission"))
    _section_heading(
        "04 · Safety boundary",
        "Deterministic controls remain in charge",
        "The public container has a read-only database mount, no broker/model credentials, and no mutation callbacks.",
    )
    columns = st.columns(5)
    columns[0].metric(
        "Kill switch",
        "ON" if kill.get("enabled") else "OFF" if kill.get("known") else "UNKNOWN",
    )
    columns[1].metric("Entry permit", permission.get("state", "MISSING"))
    columns[2].metric(
        "Max defined loss", _money(safety.get("maximum_defined_loss"))
    )
    columns[3].metric("Max contracts", safety.get("maximum_contracts", 1))
    columns[4].metric(
        "Equity kill floor", _money(safety.get("equity_kill_threshold"))
    )
    st.info(
        "Public evidence only: no order button, no kill-switch control, no Alpaca key, and no Featherless key exists in this viewer."
    )
    permission_history = _sequence(safety.get("entry_permission_history"))
    if permission_history:
        with st.expander("Date-bound entry permission history", expanded=False):
            st.dataframe(permission_history, width="stretch", hide_index=True)
    kill_history = _sequence(kill.get("history"))
    if kill_history:
        with st.expander("Kill-switch audit history", expanded=False):
            st.dataframe(kill_history, width="stretch", hide_index=True)


def _render_limitations(view: Mapping[str, Any]) -> None:
    data_profile = _mapping(view.get("data_profile"))
    _section_heading(
        "05 · Evidence boundary",
        "What this dashboard does—and does not—prove",
        "This is transparent competition evidence, not a claim of live-market profitability.",
    )
    st.warning(
        "PAPER TRADING · BASIC INDICATIVE OPTIONS DATA · SIMULATED FILLS"
    )
    limitations = [
        *(_sequence(data_profile.get("limitations"))),
        *(_sequence(view.get("limitations"))),
    ]
    for limitation in dict.fromkeys(str(item) for item in limitations):
        st.markdown(f"- {limitation}")
    st.markdown(
        "- Earnings and macro events can move the underlying beyond the modeled range.\n"
        "- Historical or paper performance does not guarantee future results.\n"
        "- This system is a bounded engineering demonstration, not investment advice."
    )
    with st.expander("Audit digest", expanded=False):
        st.code(str(view.get("report_digest") or "unavailable"), language=None)


def _section_heading(kicker: str, title: str, description: str) -> None:
    st.markdown(
        "<div class=\"tt-section\">"
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
    return max(0.0, (current.astimezone(UTC) - observed.astimezone(UTC)).total_seconds())


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
