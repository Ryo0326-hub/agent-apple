"""Credential-free Streamlit operations console for ThetaTrap."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import streamlit as st

from thetatrap.report import ReportUnavailable, build_operational_report
from thetatrap.storage import StorageInvariantError, Store


CLEAR_CONFIRMATION = "CLEAR KILL SWITCH"
BUILD_SHA_ENV = "THETATRAP_BUILD_SHA"


def display_build_sha(raw: str | None = None) -> str:
    """Render only a bounded Git revision, never arbitrary environment text."""

    value = (raw if raw is not None else os.environ.get(BUILD_SHA_ENV, "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        return "unversioned"
    return value.lower()[:12]


def update_kill_switch(
    database_path: str | Path,
    *,
    enabled: bool,
    reason: str,
    expected_version: int | None = None,
    clear_confirmation: str | None = None,
) -> dict[str, Any]:
    """Apply an operator kill-switch request without loading credentials.

    Enabling is immediate and moves a non-terminal focus run to ``RISK_OFF``.
    Clearing requires both the displayed optimistic-lock version and an exact
    confirmation phrase; it does not itself resume or submit an order.
    """

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise ReportUnavailable(f"ThetaTrap database does not exist: {path}")
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise ValueError("an operator reason is required")

    store = Store(path)
    current = store.get_kill_switch()
    if enabled:
        if current["kill_switch_enabled"]:
            return current
        report = build_operational_report(path)
        strategy = _mapping(report.get("strategy"))
        run_id = strategy.get("focus_run_id")
        return store.activate_kill_switch(
            cleaned_reason,
            "dashboard_operator",
            run_id=str(run_id) if run_id else None,
            evidence={"source": "loopback_dashboard"},
        )

    if clear_confirmation != CLEAR_CONFIRMATION:
        raise ValueError(f'type "{CLEAR_CONFIRMATION}" to clear the kill switch')
    if expected_version is None:
        raise ValueError("kill-switch version is required to clear")
    return store.clear_kill_switch(
        cleaned_reason,
        "dashboard_operator",
        expected_version=expected_version,
    )


def main() -> None:
    st.set_page_config(page_title="ThetaTrap", page_icon="🛡️", layout="wide")
    build_sha = display_build_sha()
    st.title("ThetaTrap operations")
    st.caption(
        "MCP-native Alpaca paper-options agent · private credential-free operator console"
    )
    st.caption(f"Build `{build_sha}`")

    database_path = Path(
        os.environ.get("THETATRAP_DATABASE_PATH", "data/dev/thetatrap.sqlite3")
    )
    execution_enabled = _env_bool("THETATRAP_EXECUTION_ENABLED", False)
    read_only = _env_bool("THETATRAP_READ_ONLY", True)
    environment = os.environ.get("THETATRAP_ENVIRONMENT")

    try:
        report = build_operational_report(
            database_path,
            execution_enabled=execution_enabled,
            read_only=read_only,
            environment=environment,
        )
    except ReportUnavailable as exc:
        st.warning(str(exc))
        st.stop()

    mode = _mapping(report["mode"])
    kill = _mapping(report["kill_switch"])
    health = _mapping(report["health"])
    identity = _mapping(report["identity"])
    strategy = _mapping(report["strategy"])
    one_shot_entry = _mapping(report.get("one_shot_entry"))

    banner = str(mode.get("banner", "PAPER · TRADING DISARMED"))
    if mode.get("trading_state") == "ARMED":
        st.error(f"⚠️ {banner} · automated entries can be submitted")
    else:
        st.info(f"🛡️ {banner} · broker entries cannot be submitted")
    st.caption(
        "One-shot entry permission: "
        f"{one_shot_entry.get('state', 'MISSING')}"
        + (
            f" · strategy date {one_shot_entry.get('strategy_date')}"
            if one_shot_entry.get("strategy_date")
            else ""
        )
        + " · exits remain governed by the persisted position lifecycle"
    )
    if mode.get("replay"):
        st.warning("REPLAY MODE · simulated fixtures only · broker mutations prohibited")
    st.caption(
        "Alpaca Basic indicative option quotes · paper fills are simulated and are not live-profit evidence"
    )

    columns = st.columns(6)
    columns[0].metric("Environment", str(mode.get("environment", "unknown")).upper())
    columns[1].metric("Account", identity.get("account_suffix", "unverified"))
    columns[2].metric("Worker", str(health.get("status", "waiting")).upper())
    market_state = health.get("market_is_open")
    columns[3].metric(
        "Market", "OPEN" if market_state is True else "CLOSED" if market_state is False else "UNKNOWN"
    )
    focus_run = _mapping(strategy.get("current_run")) or _mapping(strategy.get("last_run"))
    columns[4].metric("Strategy", focus_run.get("state", "NOT STARTED"))
    columns[5].metric("Kill switch", "ON" if kill.get("enabled") else "OFF")

    _render_kill_switch(database_path, kill)
    _render_runtime(report)
    _render_strategy(report)
    _render_agent(report)
    _render_orders(report)
    _render_portfolio(report)
    _render_audit(report)


def _render_kill_switch(database_path: Path, kill: Mapping[str, Any]) -> None:
    st.subheader("Emergency control")
    if not kill.get("known"):
        st.error("Kill-switch state is unavailable. Trading is treated as disarmed.")
        return

    if kill.get("enabled"):
        st.error(
            f"KILL SWITCH ACTIVE · {kill.get('reason') or 'no reason recorded'} · "
            f"version {kill.get('version')}"
        )
        with st.expander("Clear kill switch", expanded=False):
            st.warning(
                "Clearing permits a future fresh policy cycle; it never approves or immediately submits an order."
            )
            reason = st.text_input("Clear reason", key="kill_clear_reason")
            confirmation = st.text_input(
                f'Type "{CLEAR_CONFIRMATION}"', key="kill_clear_confirmation"
            )
            if st.button("Clear kill switch", type="secondary"):
                try:
                    update_kill_switch(
                        database_path,
                        enabled=False,
                        reason=reason,
                        expected_version=int(kill["version"]),
                        clear_confirmation=confirmation,
                    )
                except (ValueError, StorageInvariantError, ReportUnavailable, sqlite3.Error) as exc:
                    st.error(f"Kill switch was not cleared: {exc}")
                else:
                    st.success("Kill switch cleared. Fresh admission checks are still required.")
                    st.rerun()
    else:
        st.success("Kill switch is off.")
        reason = st.text_input("Emergency-stop reason", key="kill_enable_reason")
        if st.button("Activate kill switch", type="primary"):
            try:
                update_kill_switch(
                    database_path,
                    enabled=True,
                    reason=reason,
                )
            except (ValueError, StorageInvariantError, ReportUnavailable, sqlite3.Error) as exc:
                st.error(f"Kill switch was not activated: {exc}")
            else:
                st.success(
                    "Kill switch activated. The worker owns broker cancellation and risk-off reconciliation."
                )
                st.rerun()
    st.caption(
        "This console contains no Alpaca or Featherless keys and must remain loopback-only behind an SSH tunnel."
    )


def _render_runtime(report: Mapping[str, Any]) -> None:
    st.subheader("Runtime and MCP health")
    health = _mapping(report.get("health"))
    mcp = _mapping(report.get("mcp"))
    one_shot_entry = _mapping(report.get("one_shot_entry"))
    session = _mapping(mcp.get("latest_session"))
    columns = st.columns(5)
    columns[0].metric("Last heartbeat", _short_time(health.get("observed_at")))
    columns[1].metric("MCP", str(session.get("status", "not connected")).upper())
    columns[2].metric("MCP tools", session.get("tool_count", "n/a"))
    columns[3].metric("MCP package", session.get("package_version", "n/a"))
    columns[4].metric("One-shot entry", one_shot_entry.get("state", "MISSING"))
    with st.expander("Heartbeat and schema evidence", expanded=False):
        st.json(
            {
                "heartbeat": health,
                "latest_session": session or None,
                "last_successful_mcp_call": mcp.get("last_successful_call"),
                "one_shot_entry": one_shot_entry,
            }
        )
    calls = mcp.get("recent_calls")
    if isinstance(calls, list) and calls:
        st.dataframe(calls[:20], use_container_width=True, hide_index=True)


def _render_strategy(report: Mapping[str, Any]) -> None:
    st.subheader("Strategy run and deterministic candidate")
    strategy = _mapping(report.get("strategy"))
    current = _mapping(strategy.get("current_run"))
    last = _mapping(strategy.get("last_run"))
    columns = st.columns(2)
    columns[0].markdown("**Current active run**")
    columns[0].json(current or {"state": "none"})
    columns[1].markdown("**Most recently updated run**")
    columns[1].json(last or {"state": "none"})
    if strategy.get("no_trade_reason"):
        st.warning(f"NO_TRADE · {strategy['no_trade_reason']}")

    candidate_section = _mapping(report.get("candidate"))
    candidate = _mapping(candidate_section.get("selected"))
    if not candidate:
        st.info("No candidate has been persisted for the focus run.")
    else:
        payload = _mapping(candidate.get("payload"))
        metrics = st.columns(6)
        metrics[0].metric("Symbol", candidate.get("symbol", "n/a"))
        metrics[1].metric("Rank", candidate.get("candidate_rank", "n/a"))
        metrics[2].metric("Eligible", "YES" if candidate.get("eligible") else "NO")
        metrics[3].metric("IV ratio", payload.get("iv_ratio", "n/a"))
        metrics[4].metric("Credit", payload.get("proposed_credit", "n/a"))
        metrics[5].metric(
            "Max loss", payload.get("maximum_loss", payload.get("max_loss", "n/a"))
        )
        with st.expander("Candidate legs, prices, risk, and quote timestamps", expanded=True):
            st.json(payload)

    gates = candidate_section.get("latest_gate_outcomes")
    st.markdown("**Latest gate outcomes**")
    if isinstance(gates, list) and gates:
        st.dataframe(
            [
                {
                    "gate": gate.get("gate_name"),
                    "passed": gate.get("passed"),
                    "reason": gate.get("reason_code"),
                    "detail": gate.get("detail"),
                    "evaluated_at": gate.get("evaluated_at"),
                }
                for gate in gates
                if isinstance(gate, Mapping)
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No gate evaluation has been persisted for the selected candidate.")

    transitions = strategy.get("transitions")
    with st.expander("State transition audit", expanded=False):
        if isinstance(transitions, list) and transitions:
            st.dataframe(transitions, use_container_width=True, hide_index=True)
        else:
            st.info("No strategy transitions recorded.")


def _render_agent(report: Mapping[str, Any]) -> None:
    st.subheader("Qwen decision and bounded tool trace")
    agent = _mapping(report.get("agent"))
    latest = _mapping(agent.get("latest_run"))
    if not latest:
        st.info("No Qwen review has been persisted for the focus run.")
        return
    result = _mapping(latest.get("result"))
    metrics = st.columns(5)
    metrics[0].metric("Status", latest.get("status", "unknown"))
    metrics[1].metric("Model", latest.get("model", "unknown"))
    metrics[2].metric("Decision", result.get("decision", result.get("outcome", "n/a")))
    metrics[3].metric("Veto", latest.get("veto_reason") or "none")
    metrics[4].metric("Error", latest.get("error_type") or "none")
    trace = agent.get("tool_trace")
    if isinstance(trace, list) and trace:
        st.dataframe(
            [
                {
                    "sequence": item.get("sequence"),
                    "kind": "official MCP" if item.get("is_official_mcp") else "local",
                    "tool": item.get("tool_name"),
                    "status": item.get("status"),
                    "duration_ms": item.get("duration_ms"),
                    "arguments": item.get("arguments"),
                    "result_summary": item.get("result_summary"),
                    "called_at": item.get("called_at"),
                }
                for item in trace
                if isinstance(item, Mapping)
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No agent tool calls recorded.")
    with st.expander("Agent result and audit hashes", expanded=False):
        st.json(
            {
                "result": result or None,
                "prompt_hash": latest.get("prompt_hash"),
                "config_hash": latest.get("config_hash"),
                "started_at": latest.get("started_at"),
                "ended_at": latest.get("ended_at"),
            }
        )


def _render_orders(report: Mapping[str, Any]) -> None:
    st.subheader("Order chains, broker status, and fills")
    orders = _mapping(report.get("orders"))
    metrics = st.columns(3)
    metrics[0].metric("Logical chains", orders.get("chain_count", 0))
    metrics[1].metric("Leg fills", orders.get("fill_count", 0))
    metrics[2].metric(
        "Option cash flow, ex-fees", f"${orders.get('option_cash_flow_ex_fees', '0.00')}"
    )
    chains = orders.get("chains")
    if not isinstance(chains, list) or not chains:
        st.info("No order intent has been submitted for the focus run.")
        return
    for chain_value in chains:
        if not isinstance(chain_value, Mapping):
            continue
        chain = _mapping(chain_value)
        with st.expander(
            f"{str(chain.get('purpose', 'order')).upper()} · {chain.get('state')} · {chain.get('chain_id')}",
            expanded=True,
        ):
            st.json(
                {
                    "client_order_id": chain.get("client_order_id"),
                    "payload_hash": chain.get("payload_hash"),
                    "intent": chain.get("payload"),
                }
            )
            attempts = chain.get("attempts")
            if isinstance(attempts, list) and attempts:
                st.markdown("**Attempts**")
                st.dataframe(attempts, use_container_width=True, hide_index=True)
            history = chain.get("status_history")
            if isinstance(history, list) and history:
                st.markdown("**Status history**")
                st.dataframe(history, use_container_width=True, hide_index=True)
            fills = chain.get("fills")
            if isinstance(fills, list) and fills:
                st.markdown("**Fills**")
                st.dataframe(fills, use_container_width=True, hide_index=True)


def _render_portfolio(report: Mapping[str, Any]) -> None:
    st.subheader("Positions and account equity")
    portfolio = _mapping(report.get("portfolio"))
    position = _mapping(portfolio.get("latest_position_observation"))
    equity = _mapping(portfolio.get("equity"))
    columns = st.columns(5)
    columns[0].metric(
        "Position", "FLAT" if position.get("is_flat") is True else "OPEN" if position else "UNKNOWN"
    )
    columns[1].metric("First equity", _format_money(equity.get("first")))
    columns[2].metric("Latest equity", _format_money(equity.get("latest")))
    columns[3].metric("Observed change", _format_money(equity.get("observed_change"), signed=True))
    columns[4].metric("Equity observed", _short_time(equity.get("latest_observed_at")))
    if position:
        with st.expander("Latest broker position observation", expanded=True):
            st.json(position)
    history = equity.get("history")
    if isinstance(history, list) and history:
        st.dataframe(history, use_container_width=True, hide_index=True)
    else:
        st.info("No timestamped equity observation has been persisted.")


def _render_audit(report: Mapping[str, Any]) -> None:
    st.subheader("Audit boundary")
    for limitation in report.get("limitations", []):
        st.caption(f"• {limitation}")
    st.code(str(report.get("report_digest", "unavailable")), language=None)


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


def _short_time(value: Any) -> str:
    if not value:
        return "not observed"
    text = str(value)
    return text.replace("T", " ").replace("+00:00", " UTC")[:23]


def _format_money(value: Any, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(str(value))
    except ValueError:
        return "n/a"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}${number:,.2f}"


if __name__ == "__main__":
    main()
