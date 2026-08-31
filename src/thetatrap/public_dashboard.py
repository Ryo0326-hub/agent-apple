"""Public, read-only evidence dashboard with no operator mutation imports."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
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
    """Project the operational report into the minimum judge-facing evidence."""

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
    heartbeat_age = _heartbeat_age_seconds(health.get("observed_at"))

    chains = []
    for chain_value in _sequence(orders.get("chains")):
        chain = _mapping(chain_value)
        chains.append(
            {
                "purpose": chain.get("purpose"),
                "state": chain.get("state"),
                "attempt_count": len(_sequence(chain.get("attempts"))),
                "fill_count": len(_sequence(chain.get("fills"))),
            }
        )

    trace = []
    for item_value in _sequence(agent.get("tool_trace")):
        item = _mapping(item_value)
        trace.append(
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
        )

    public = {
        "mode": {
            "environment": mode.get("environment"),
            "paper": mode.get("paper", True),
            "data_feed": mode.get("data_feed"),
            "trading_state": mode.get("trading_state"),
            "banner": mode.get("banner"),
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
            "status": session.get("status"),
            "package_version": session.get("package_version"),
            "tool_count": session.get("tool_count"),
            "required_schema_hash": session.get("required_schema_hash"),
            "last_successful_call_at": _mapping(
                mcp.get("last_successful_call")
            ).get("called_at"),
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
        },
        "candidate": {
            "symbol": candidate.get("symbol"),
            "rank": candidate.get("candidate_rank"),
            "eligible": candidate.get("eligible"),
            "payload": candidate.get("payload"),
            "gates": candidate_section.get("latest_gate_outcomes", []),
        },
        "agent": {
            "model": latest_agent.get("model"),
            "status": latest_agent.get("status"),
            "decision": _mapping(latest_agent.get("result")).get(
                "decision", latest_agent.get("veto_reason")
            ),
            "veto_reason": latest_agent.get("veto_reason"),
            "error_type": latest_agent.get("error_type"),
            "tool_trace": trace,
        },
        "orders": {
            "chain_count": orders.get("chain_count", 0),
            "fill_count": orders.get("fill_count", 0),
            "option_cash_flow_ex_fees": orders.get(
                "option_cash_flow_ex_fees", "0.00"
            ),
            "chains": chains,
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
        },
        "limitations": report.get("limitations", []),
        "report_digest": report.get("report_digest"),
    }
    return redact_public_identifiers(public)


def main() -> None:
    st.set_page_config(page_title="ThetaTrap evidence", page_icon="🛡️", layout="wide")
    st.markdown(
        """
        <style>
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        #MainMenu,
        footer { visibility: hidden; height: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("ThetaTrap paper-trading evidence")
    st.caption(
        "Public read-only dashboard · no broker controls · no Alpaca or Featherless credentials"
    )
    st.caption(f"Build `{display_build_sha()}`")

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
    _render_limitations(view)


def _render_status(view: Mapping[str, Any]) -> None:
    mode = _mapping(view.get("mode"))
    health = _mapping(view.get("health"))
    mcp = _mapping(view.get("mcp"))
    emergency = _mapping(view.get("emergency"))
    account = _mapping(view.get("account"))
    permission = _mapping(view.get("one_shot_entry"))

    banner = str(mode.get("banner") or "PAPER · TRADING DISARMED")
    if mode.get("trading_state") == "ARMED":
        st.error(f"⚠️ {banner} · automated paper entries may be submitted")
    else:
        st.info(f"🛡️ {banner} · new broker entries are blocked")
    st.caption(
        "Alpaca Basic indicative option quotes · paper fills are simulated and are not live-profit evidence"
    )

    columns = st.columns(6)
    columns[0].metric("Environment", str(mode.get("environment", "unknown")).upper())
    columns[1].metric("Account", account.get("suffix", "unverified"))
    columns[2].metric("Worker", str(health.get("status", "unknown")).upper())
    columns[3].metric(
        "Market",
        "OPEN"
        if health.get("market_is_open") is True
        else "CLOSED"
        if health.get("market_is_open") is False
        else "UNKNOWN",
    )
    columns[4].metric("MCP", str(mcp.get("status", "unknown")).upper())
    columns[5].metric(
        "Kill switch",
        "ON"
        if emergency.get("enabled")
        else "OFF"
        if emergency.get("known")
        else "UNKNOWN",
    )
    st.caption(
        f"One-shot entry: {permission.get('state', 'MISSING')}"
        + (
            f" · strategy date {permission.get('strategy_date')}"
            if permission.get("strategy_date")
            else ""
        )
        + f" · last heartbeat {health.get('observed_at') or 'not observed'}"
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
    with st.expander("MCP evidence", expanded=False):
        st.json(mcp)


def _render_strategy(view: Mapping[str, Any]) -> None:
    strategy = _mapping(view.get("strategy"))
    candidate = _mapping(view.get("candidate"))
    payload = _mapping(candidate.get("payload"))
    st.subheader("Strategy and deterministic candidate")
    columns = st.columns(4)
    columns[0].metric("State", strategy.get("state", "NOT_STARTED"))
    columns[1].metric("Date", strategy.get("strategy_date") or "none")
    columns[2].metric("Symbol", candidate.get("symbol") or "none")
    columns[3].metric(
        "Eligible",
        "YES" if candidate.get("eligible") is True else "NO" if candidate else "N/A",
    )
    if strategy.get("no_trade_reason"):
        st.warning(f"NO_TRADE · {strategy['no_trade_reason']}")
    if payload:
        st.json(payload)
    gates = _sequence(candidate.get("gates"))
    if gates:
        st.dataframe(gates, use_container_width=True, hide_index=True)
    else:
        st.info("No deterministic gate result has been published yet.")


def _render_agent(view: Mapping[str, Any]) -> None:
    agent = _mapping(view.get("agent"))
    st.subheader("Qwen decision and bounded tool trace")
    columns = st.columns(4)
    columns[0].metric("Model", agent.get("model") or "not run")
    columns[1].metric("Status", agent.get("status") or "not run")
    columns[2].metric("Decision", agent.get("decision") or "none")
    columns[3].metric("Error", agent.get("error_type") or "none")
    trace = _sequence(agent.get("tool_trace"))
    if trace:
        st.dataframe(trace, use_container_width=True, hide_index=True)
    else:
        st.info("No redacted Qwen tool trace has been published yet.")


def _render_orders_and_portfolio(view: Mapping[str, Any]) -> None:
    orders = _mapping(view.get("orders"))
    portfolio = _mapping(view.get("portfolio"))
    st.subheader("Paper orders, fills, and P&L evidence")
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
        st.dataframe(chains, use_container_width=True, hide_index=True)
    else:
        st.info("No paper order chain has been published yet.")
    st.caption(
        f"First observed equity {_money(portfolio.get('first_equity'))} · "
        f"latest observation {portfolio.get('observed_at') or 'not observed'}"
    )


def _render_limitations(view: Mapping[str, Any]) -> None:
    st.subheader("Evidence boundary")
    for limitation in _sequence(view.get("limitations")):
        st.caption(f"• {limitation}")
    st.code(str(view.get("report_digest") or "unavailable"), language=None)


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


if __name__ == "__main__":
    main()
