"""Read-only operational and final-run reporting for ThetaTrap.

The report builder deliberately reads SQLite directly in query-only mode.  It
never loads broker/model credentials and never starts an MCP session.  The same
structured report drives the local Streamlit dashboard and the export used for
the hackathon audit trail.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from thetatrap.data_profile import ALPACA_BASIC_INDICATIVE
from thetatrap.security import redact
from thetatrap.settings import account_suffix


REPORT_SCHEMA_VERSION = "2"
REPORT_DATA_FEED = "BASIC INDICATIVE"
MAX_RECENT_ROWS = 100


class ReportUnavailable(RuntimeError):
    """Raised when no initialized ThetaTrap database can be read."""


def build_operational_report(
    database_path: str | Path,
    *,
    execution_enabled: bool = False,
    read_only: bool = True,
    environment: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one credential-free, JSON-serializable operational report.

    ``execution_enabled`` and ``read_only`` must be supplied from the worker's
    deployment configuration.  They are intentionally not inferred from
    broker activity, because an empty account does not prove that execution is
    disarmed.
    """

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise ReportUnavailable(f"ThetaTrap database does not exist: {path}")

    when = _aware_utc(generated_at)
    connection = _open_read_only(path)
    try:
        tables = _table_names(connection)
        if "metadata" not in tables:
            raise ReportUnavailable("ThetaTrap database has not been initialized")

        metadata = _metadata(connection)
        heartbeat = _latest_heartbeat(connection, tables)
        mcp = _mcp_summary(connection, tables)
        runs = _strategy_runs(connection, tables)
        current_run = next((run for run in runs if run["finished_at"] is None), None)
        last_run = runs[0] if runs else None
        focus_run = current_run or last_run
        focus_run_id = str(focus_run["run_id"]) if focus_run else None
        transitions = _strategy_transitions(connection, tables, focus_run_id)
        transition_history = _all_strategy_transitions(connection, tables)
        candidates, selected_candidate, latest_gates = _candidate_summary(
            connection, tables, focus_run_id
        )
        candidate_history = _all_candidates(connection, tables)
        scan_matrix = _scan_matrix(connection, tables, candidate_history)
        agent = _agent_summary(connection, tables, focus_run_id)
        orders = _order_summary(connection, tables, focus_run_id)
        portfolio = _portfolio_summary(connection, tables, focus_run_id)
        kill_switch = _kill_switch_summary(connection, tables)
        entry_permission_history = _entry_permission_history(connection, tables)

        resolved_environment = (
            environment
            or _as_string(heartbeat.get("environment"))
            or metadata.get("environment")
            or _as_string(last_run.get("environment") if last_run else None)
            or "unknown"
        )
        one_shot_entry = _entry_permission_summary(
            connection,
            tables,
            environment=resolved_environment,
            account_id=metadata.get("account_id"),
            now=when,
        )
        replay = resolved_environment.lower() == "replay"
        kill_active = bool(kill_switch.get("enabled")) or not bool(
            kill_switch.get("known")
        )
        trading_disarmed = (
            replay
            or read_only
            or not execution_enabled
            or kill_active
            or not bool(one_shot_entry.get("active"))
        )
        trading_state = "DISARMED" if trading_disarmed else "ARMED"

        suffix = _as_string(heartbeat.get("account_suffix"))
        if not suffix and metadata.get("account_id"):
            suffix = account_suffix(metadata["account_id"])

        no_trade_reason = None
        if focus_run and focus_run.get("state") == "NO_TRADE":
            no_trade_reason = _latest_reason(transitions)

        report: dict[str, Any] = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": when.isoformat(),
            "mode": {
                "environment": resolved_environment,
                "paper": True,
                "data_feed": REPORT_DATA_FEED,
                "read_only": bool(read_only),
                "execution_enabled": bool(execution_enabled),
                "replay": replay,
                "trading_state": trading_state,
                "banner": f"PAPER · TRADING {trading_state}",
            },
            "identity": {
                "account_suffix": suffix or "unverified",
                "database_environment_bound": bool(metadata.get("environment")),
                "database_account_bound": bool(metadata.get("account_id")),
            },
            "health": heartbeat,
            "mcp": mcp,
            "kill_switch": kill_switch,
            "one_shot_entry": one_shot_entry,
            "strategy": {
                "current_run": current_run,
                "last_run": last_run,
                "focus_run_id": focus_run_id,
                "transitions": transitions,
                "transition_history": transition_history,
                "no_trade_reason": no_trade_reason,
                "run_history": runs,
            },
            "candidate": {
                "selected": selected_candidate,
                "latest_gate_outcomes": latest_gates,
                "all_for_focus_run": candidates,
                "history": candidate_history,
                "scan_matrix": scan_matrix,
            },
            "agent": agent,
            "orders": orders,
            "portfolio": portfolio,
            "safety": {
                "kill_switch": kill_switch,
                "entry_permissions": entry_permission_history,
                "paper_only": True,
                "read_only_viewer": True,
                "maximum_defined_loss": "500.00",
                "maximum_contracts": 1,
                "equity_kill_threshold": "99000.00",
            },
            "source_labels": {
                "quotes": "Alpaca Basic indicative options data",
                "execution": "Alpaca paper trading (simulated fills)",
                "agent": "Featherless-hosted open-source Qwen orchestration",
            },
            "data_profile": ALPACA_BASIC_INDICATIVE.status(),
            "limitations": [
                "Indicative option quotes are not consolidated live OPRA quotes.",
                "Paper fills are simulated and do not establish live profitability.",
                "Observed equity change is authoritative for the submitted paper account; fill cash flow excludes fees and assignment effects.",
            ],
        }
        safe_report = _sanitize_for_report(report)
        safe_report["report_digest"] = _digest(safe_report)
        return safe_report
    except sqlite3.DatabaseError as exc:
        raise ReportUnavailable(f"unable to read ThetaTrap database: {exc}") from exc
    finally:
        connection.close()


def generate_report(
    database_path: str | Path,
    output_path: str | Path,
    *,
    execution_enabled: bool = False,
    read_only: bool = True,
    environment: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build and write a deterministic JSON or Markdown report.

    The extension must be ``.json``, ``.md``, or ``.markdown``.  Existing files
    are replaced only when the caller explicitly supplies that output path.
    """

    report = build_operational_report(
        database_path,
        execution_enabled=execution_enabled,
        read_only=read_only,
        environment=environment,
        generated_at=generated_at,
    )
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.lower()
    if suffix == ".json":
        content = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    elif suffix in {".md", ".markdown"}:
        content = render_markdown(report)
    else:
        raise ValueError("report output must use .json, .md, or .markdown")
    output.write_text(content, encoding="utf-8")
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the structured report as a concise, judge-readable Markdown file."""

    mode = _mapping(report.get("mode"))
    identity = _mapping(report.get("identity"))
    health = _mapping(report.get("health"))
    kill = _mapping(report.get("kill_switch"))
    one_shot_entry = _mapping(report.get("one_shot_entry"))
    strategy = _mapping(report.get("strategy"))
    current_run = _mapping(strategy.get("current_run"))
    last_run = _mapping(strategy.get("last_run"))
    focus_run = current_run or last_run
    candidate_section = _mapping(report.get("candidate"))
    candidate = _mapping(candidate_section.get("selected"))
    candidate_payload = _mapping(candidate.get("payload"))
    agent = _mapping(report.get("agent"))
    orders = _mapping(report.get("orders"))
    portfolio = _mapping(report.get("portfolio"))
    equity = _mapping(portfolio.get("equity"))

    lines = [
        "# ThetaTrap final run report",
        "",
        f"> **{_md(mode.get('banner', 'PAPER · TRADING DISARMED'))}**",
        "",
        f"Generated: `{_md(report.get('generated_at'))}`  ",
        f"Environment: `{_md(mode.get('environment'))}`  ",
        f"Data: `{_md(mode.get('data_feed'))}`  ",
        f"Account: `{_md(identity.get('account_suffix'))}`",
        "",
        "## Outcome",
        "",
        f"- Strategy state: `{_md(focus_run.get('state', 'NOT_STARTED'))}`",
        f"- Strategy run: `{_md(focus_run.get('run_id', 'none'))}`",
        f"- No-trade reason: `{_md(strategy.get('no_trade_reason') or 'none')}`",
        f"- Kill switch: `{'ON' if kill.get('enabled') else 'OFF' if kill.get('known') else 'UNKNOWN'}`",
        f"- One-shot entry: `{_md(one_shot_entry.get('state', 'MISSING'))}` for `{_md(one_shot_entry.get('strategy_date', 'no date'))}`",
        f"- Worker: `{_md(health.get('status', 'unknown'))}` at `{_md(health.get('observed_at', 'not observed'))}`",
        "",
        "## Account equity",
        "",
        f"- First observed equity: `{_money(equity.get('first'))}`",
        f"- Latest observed equity: `{_money(equity.get('latest'))}`",
        f"- Observed-window change: `{_money(equity.get('observed_change'), signed=True)}`",
        f"- Latest observation: `{_md(equity.get('latest_observed_at', 'none'))}`",
        "",
        "## Candidate and deterministic gates",
        "",
    ]
    if candidate:
        lines.extend(
            [
                f"Selected `{_md(candidate.get('symbol'))}` (rank `{_md(candidate.get('candidate_rank'))}`, eligible `{_md(candidate.get('eligible'))}`).",
                "",
                f"- Expected move: `{_md(_first_present(candidate_payload, 'expected_move', 'expected_move_fraction'))}`",
                f"- IV ratio: `{_md(candidate_payload.get('iv_ratio', 'n/a'))}`",
                f"- Proposed credit: `{_md(candidate_payload.get('proposed_credit', 'n/a'))}`",
                f"- Maximum loss: `{_md(candidate_payload.get('maximum_loss', candidate_payload.get('max_loss', 'n/a')))}`",
                "",
            ]
        )
    else:
        lines.extend(["No candidate was persisted for the focus run.", ""])

    gates = _sequence(candidate_section.get("latest_gate_outcomes"))
    lines.extend(_markdown_table(
        ["Gate", "Passed", "Reason", "Evaluated"],
        [
            [
                gate.get("gate_name"),
                "yes" if gate.get("passed") else "no",
                gate.get("reason_code") or "-",
                gate.get("evaluated_at"),
            ]
            for gate in gates
            if isinstance(gate, Mapping)
        ],
    ))

    lines.extend(["", "## Qwen decision and tool trace", ""])
    latest_agent = _mapping(agent.get("latest_run"))
    if latest_agent:
        lines.extend(
            [
                f"- Model: `{_md(latest_agent.get('model'))}`",
                f"- Status: `{_md(latest_agent.get('status'))}`",
                f"- Decision: `{_md(_mapping(latest_agent.get('result')).get('decision', latest_agent.get('veto_reason') or 'not recorded'))}`",
                f"- Error: `{_md(latest_agent.get('error_type') or 'none')}`",
                "",
            ]
        )
    else:
        lines.extend(["No LLM review was persisted for the focus run.", ""])
    trace = _sequence(agent.get("tool_trace"))
    lines.extend(_markdown_table(
        ["#", "Kind", "Tool", "Status", "Duration ms"],
        [
            [
                item.get("sequence"),
                "official MCP" if item.get("is_official_mcp") else "local",
                item.get("tool_name"),
                item.get("status"),
                item.get("duration_ms"),
            ]
            for item in trace
            if isinstance(item, Mapping)
        ],
    ))

    lines.extend(["", "## Orders and fills", ""])
    lines.append(
        f"Order chains: `{orders.get('chain_count', 0)}`; fills: `{orders.get('fill_count', 0)}`; "
        f"option cash flow before fees: `{_money(orders.get('option_cash_flow_ex_fees'), signed=True)}`."
    )
    lines.append("")
    lines.extend(_markdown_table(
        ["Purpose", "Chain", "State", "Attempts", "Fills"],
        [
            [
                chain.get("purpose"),
                chain.get("chain_id"),
                chain.get("state"),
                len(_sequence(chain.get("attempts"))),
                len(_sequence(chain.get("fills"))),
            ]
            for chain in _sequence(orders.get("chains"))
            if isinstance(chain, Mapping)
        ],
    ))

    latest_position = _mapping(portfolio.get("latest_position_observation"))
    lines.extend(
        [
            "",
            "## Position reconciliation",
            "",
            f"- Flat: `{_md(latest_position.get('is_flat', 'unknown'))}`",
            f"- Observed: `{_md(latest_position.get('observed_at', 'none'))}`",
            "",
            "## Audit notes",
            "",
        ]
    )
    run_history = _sequence(strategy.get("run_history"))
    if run_history:
        lines.extend(
            [
                "### Strategy-day history",
                "",
                *_markdown_table(
                    ["Date", "Run", "State", "Environment", "Updated"],
                    [
                        [
                            item.get("strategy_date"),
                            item.get("run_id"),
                            item.get("state"),
                            item.get("environment"),
                            item.get("updated_at"),
                        ]
                        for item in run_history
                        if isinstance(item, Mapping)
                    ],
                ),
                "",
            ]
        )
    for limitation in _sequence(report.get("limitations")):
        lines.append(f"- {_md(limitation)}")
    lines.extend(
        [
            f"- Report digest: `{_md(report.get('report_digest'))}`",
            "",
        ]
    )
    return "\n".join(lines)


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM metadata").fetchall()
    }


def _latest_heartbeat(
    connection: sqlite3.Connection, tables: set[str]
) -> dict[str, Any]:
    if "worker_heartbeat" not in tables:
        return {"status": "not_initialized", "detail": {}}
    row = connection.execute(
        "SELECT * FROM worker_heartbeat WHERE singleton_id=1"
    ).fetchone()
    if row is None:
        return {"status": "waiting", "detail": {}}
    result = dict(row)
    result["market_is_open"] = (
        bool(result["market_is_open"])
        if result.get("market_is_open") is not None
        else None
    )
    result["detail"] = _decode_json(result.pop("detail_json", None), {})
    return result


def _mcp_summary(
    connection: sqlite3.Connection, tables: set[str]
) -> dict[str, Any]:
    session: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []
    if "mcp_sessions" in tables:
        row = connection.execute(
            "SELECT * FROM mcp_sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        session = dict(row) if row else None
    if "mcp_calls" in tables:
        rows = connection.execute(
            """
            SELECT called_at, principal, tool_name, status, duration_ms
            FROM mcp_calls ORDER BY id DESC
            """
        ).fetchall()
        calls = [dict(row) for row in rows]
    last_successful_call = next(
        (call for call in calls if str(call.get("status", "")).lower() == "ok"),
        None,
    )
    raw_status = str((session or {}).get("status") or "").lower()
    operational_status = (
        "ready"
        if raw_status == "closed" and last_successful_call is not None
        else raw_status or "not_connected"
    )
    return {
        "latest_session": session,
        "operational_status": operational_status,
        "recent_calls": calls[:MAX_RECENT_ROWS],
        "timeline": list(reversed(calls)),
        "last_successful_call": last_successful_call,
    }


def _strategy_runs(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    if "strategy_runs" not in tables:
        return []
    rows = connection.execute(
        "SELECT * FROM strategy_runs ORDER BY updated_at DESC, created_at DESC LIMIT ?",
        (MAX_RECENT_ROWS,),
    ).fetchall()
    return [_decode_fields(row, "context_json") for row in rows]


def _strategy_transitions(
    connection: sqlite3.Connection, tables: set[str], run_id: str | None
) -> list[dict[str, Any]]:
    if not run_id or "strategy_transitions" not in tables:
        return []
    rows = connection.execute(
        "SELECT * FROM strategy_transitions WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [_decode_fields(row, "evidence_json") for row in rows]


def _all_strategy_transitions(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    if "strategy_transitions" not in tables or "strategy_runs" not in tables:
        return []
    rows = connection.execute(
        """
        SELECT transition.*, run.strategy_date, run.environment
        FROM strategy_transitions AS transition
        JOIN strategy_runs AS run ON run.run_id=transition.run_id
        ORDER BY transition.transitioned_at, transition.id
        """
    ).fetchall()
    return [_decode_fields(row, "evidence_json") for row in rows]


def _candidate_summary(
    connection: sqlite3.Connection, tables: set[str], run_id: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    if not run_id or "candidates" not in tables:
        return [], None, []
    rows = connection.execute(
        """
        SELECT * FROM candidates WHERE run_id=?
        ORDER BY candidate_rank IS NULL, candidate_rank, created_at DESC, candidate_id
        """,
        (run_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        candidate = _decode_fields(row, "payload_json")
        candidate["eligible"] = bool(candidate["eligible"])
        candidates.append(candidate)
    selected = next((item for item in candidates if item["eligible"]), None)
    if selected is None and candidates:
        selected = candidates[0]

    gates: list[dict[str, Any]] = []
    if selected and "candidate_gate_results" in tables:
        gate_rows = connection.execute(
            """
            SELECT * FROM candidate_gate_results
            WHERE candidate_id=? ORDER BY evaluated_at, id
            """,
            (selected["candidate_id"],),
        ).fetchall()
        decoded = []
        for row in gate_rows:
            gate = _decode_fields(row, "detail_json")
            gate["passed"] = bool(gate["passed"])
            decoded.append(gate)
        if decoded:
            latest_evaluation = decoded[-1]["evaluation_id"]
            gates = [
                gate for gate in decoded if gate["evaluation_id"] == latest_evaluation
            ]
    return candidates, selected, gates


def _all_candidates(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    if "candidates" not in tables:
        return []
    rows = connection.execute(
        """
        SELECT candidate.*, run.strategy_date, run.state AS run_state,
               snapshot.observed_at AS scanned_at,
               snapshot.collection_type AS collection_type
        FROM candidates AS candidate
        JOIN strategy_runs AS run ON run.run_id=candidate.run_id
        LEFT JOIN collection_snapshots AS snapshot
          ON snapshot.snapshot_id=candidate.snapshot_id
        ORDER BY COALESCE(snapshot.observed_at, candidate.created_at) DESC,
                 candidate.created_at DESC,
                 candidate.candidate_rank IS NULL,
                 candidate.candidate_rank,
                 candidate.symbol
        """
    ).fetchall()
    gates_by_candidate: dict[str, list[dict[str, Any]]] = {}
    if "candidate_gate_results" in tables:
        gate_rows = connection.execute(
            """
            SELECT * FROM candidate_gate_results
            ORDER BY evaluated_at, id
            """
        ).fetchall()
        for gate_row in gate_rows:
            gate = _decode_fields(gate_row, "detail_json")
            gate["passed"] = bool(gate["passed"])
            gates_by_candidate.setdefault(str(gate["candidate_id"]), []).append(gate)

    candidates: list[dict[str, Any]] = []
    for row in rows:
        candidate = _decode_fields(row, "payload_json")
        candidate["eligible"] = bool(candidate["eligible"])
        gates = gates_by_candidate.get(str(candidate["candidate_id"]), [])
        evaluations: list[dict[str, Any]] = []
        by_evaluation: dict[str, list[dict[str, Any]]] = {}
        for gate in gates:
            by_evaluation.setdefault(str(gate["evaluation_id"]), []).append(gate)
        for evaluation_id, evaluation_gates in by_evaluation.items():
            evaluations.append(
                {
                    "evaluation_id": evaluation_id,
                    "evaluated_at": evaluation_gates[-1].get("evaluated_at"),
                    "passed": all(bool(gate.get("passed")) for gate in evaluation_gates),
                    "gates": evaluation_gates,
                }
            )
        candidate["gate_evaluations"] = evaluations
        candidate["gates"] = gates
        candidate["failed_gate_names"] = sorted(
            {
                str(gate.get("reason_code") or gate.get("gate_name"))
                for gate in gates
                if not gate.get("passed")
            }
        )
        candidates.append(candidate)
    return candidates


def _scan_matrix(
    connection: sqlite3.Connection,
    tables: set[str],
    candidate_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if "event_definitions" not in tables:
        return []
    events = [
        dict(row)
        for row in connection.execute(
            """
            SELECT symbol, strategy_version, event_date, release_timing,
                   conference_call_at, status, exclusion_reason,
                   trade_expiration, term_expiration
            FROM event_definitions
            ORDER BY event_date, symbol
            """
        ).fetchall()
    ]
    collection_counts: dict[str, int] = {}
    if "collection_snapshots" in tables:
        collection_counts = {
            str(row["symbol"]): int(row["collection_count"])
            for row in connection.execute(
                """
                SELECT symbol, COUNT(*) AS collection_count
                FROM collection_snapshots GROUP BY symbol
                """
            ).fetchall()
        }
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidate_history:
        by_symbol.setdefault(str(candidate.get("symbol") or ""), []).append(candidate)

    matrix: list[dict[str, Any]] = []
    for event in events:
        symbol = str(event["symbol"])
        history = by_symbol.get(symbol, [])
        latest = history[0] if history else {}
        configured_status = str(event.get("status") or "unknown").upper()
        if configured_status != "VERIFIED":
            latest_result = "EXCLUDED"
        elif latest:
            latest_result = "ELIGIBLE" if latest.get("eligible") else "REJECTED"
        elif collection_counts.get(symbol, 0):
            latest_result = "COLLECTED"
        else:
            latest_result = "NOT_SCANNED"
        matrix.append(
            {
                **event,
                "configured_status": configured_status,
                "collection_count": collection_counts.get(symbol, 0),
                "evaluation_count": len(history),
                "eligible_count": sum(
                    1 for item in history if bool(item.get("eligible"))
                ),
                "latest_result": latest_result,
                "latest_scanned_at": latest.get("scanned_at") or latest.get("created_at"),
                "latest_failed_gates": latest.get("failed_gate_names", []),
                "latest_payload": latest.get("payload"),
            }
        )
    return matrix


def _agent_summary(
    connection: sqlite3.Connection, tables: set[str], run_id: str | None
) -> dict[str, Any]:
    if "agent_runs" not in tables:
        return {
            "latest_run": None,
            "tool_trace": [],
            "last_successful_call": None,
            "run_history": [],
            "reviews": [],
            "advisories": _advisory_summary(connection, tables),
        }
    history = [
        _decode_fields(item, "result_json")
        for item in connection.execute(
            """
            SELECT agent.*, candidate.symbol, candidate.candidate_rank,
                   run.strategy_date
            FROM agent_runs AS agent
            LEFT JOIN candidates AS candidate
              ON candidate.candidate_id=agent.candidate_id
            LEFT JOIN strategy_runs AS run ON run.run_id=agent.run_id
            ORDER BY COALESCE(agent.ended_at, agent.started_at) DESC,
                     agent.started_at DESC
            """
        ).fetchall()
    ]
    reviews: list[dict[str, Any]] = []
    for item in history:
        review = dict(item)
        review["tool_trace"] = _agent_tool_trace(
            connection, tables, str(item["agent_run_id"])
        )
        reviews.append(review)
    if not run_id:
        return {
            "latest_run": None,
            "tool_trace": [],
            "last_successful_call": None,
            "run_history": history,
            "reviews": reviews,
            "advisories": _advisory_summary(connection, tables),
        }
    row = connection.execute(
        """
        SELECT * FROM agent_runs WHERE run_id=?
        ORDER BY COALESCE(ended_at, started_at) DESC, started_at DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return {
            "latest_run": None,
            "tool_trace": [],
            "last_successful_call": None,
            "run_history": history,
            "reviews": reviews,
            "advisories": _advisory_summary(connection, tables),
        }
    latest = _decode_fields(row, "result_json")
    trace = _agent_tool_trace(connection, tables, str(latest["agent_run_id"]))
    return {
        "latest_run": latest,
        "tool_trace": trace,
        "last_successful_call": next(
            (item for item in reversed(trace) if str(item.get("status", "")).lower() == "ok"),
            None,
        ),
        "run_history": history,
        "reviews": reviews,
        "advisories": _advisory_summary(connection, tables),
    }


def _agent_tool_trace(
    connection: sqlite3.Connection,
    tables: set[str],
    agent_run_id: str,
) -> list[dict[str, Any]]:
    if "agent_tool_trace" not in tables:
        return []
    trace: list[dict[str, Any]] = []
    rows = connection.execute(
        """
        SELECT * FROM agent_tool_trace
        WHERE agent_run_id=? ORDER BY sequence
        """,
        (agent_run_id,),
    ).fetchall()
    for row in rows:
        item = _decode_fields(row, "arguments_json", "result_json")
        item["is_official_mcp"] = bool(item["is_official_mcp"])
        item["result_summary"] = _result_summary(item.pop("result", None))
        trace.append(item)
    return trace


def _advisory_summary(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    """Read optional rejected-candidate advisories on old and new databases."""

    if "advisory_runs" not in tables:
        return []
    rows = connection.execute(
        """
        SELECT advisory.*, candidate.symbol, candidate.candidate_rank,
               run.strategy_date
        FROM advisory_runs AS advisory
        LEFT JOIN candidates AS candidate
          ON candidate.candidate_id=advisory.candidate_id
        LEFT JOIN strategy_runs AS run ON run.run_id=advisory.run_id
        ORDER BY COALESCE(advisory.ended_at, advisory.started_at) DESC,
                 advisory.started_at DESC
        """
    ).fetchall()
    advisories: list[dict[str, Any]] = []
    for row in rows:
        item = _decode_fields(row, "result_json")
        trace: list[dict[str, Any]] = []
        if "advisory_tool_trace" in tables:
            trace = [
                dict(trace_row)
                for trace_row in connection.execute(
                    """
                    SELECT * FROM advisory_tool_trace
                    WHERE advisory_run_id=? ORDER BY sequence
                    """,
                    (item["advisory_run_id"],),
                ).fetchall()
            ]
        item["tool_trace"] = trace
        advisories.append(item)
    return advisories


def _order_summary(
    connection: sqlite3.Connection, tables: set[str], run_id: str | None
) -> dict[str, Any]:
    if "order_chains" not in tables:
        return {
            "chains": [],
            "chain_count": 0,
            "fill_count": 0,
            "option_cash_flow_ex_fees": "0.00",
        }
    chain_rows = connection.execute(
        f"""
        SELECT chain.*, intent.client_order_id, intent.payload_json,
               intent.payload_hash
        FROM order_chains AS chain
        JOIN order_intents AS intent ON intent.intent_id=chain.intent_id
        ORDER BY chain.created_at, chain.chain_id
        LIMIT {MAX_RECENT_ROWS}
        """,
    ).fetchall()
    chains: list[dict[str, Any]] = []
    cash_flow = Decimal("0")
    fill_count = 0
    for chain_row in chain_rows:
        chain = _decode_fields(chain_row, "payload_json")
        chain_id = chain["chain_id"]
        attempts: list[dict[str, Any]] = []
        if "order_attempts" in tables:
            attempts = [
                _decode_fields(row, "request_json")
                for row in connection.execute(
                    "SELECT * FROM order_attempts WHERE chain_id=? ORDER BY sequence",
                    (chain_id,),
                ).fetchall()
            ]
        history: list[dict[str, Any]] = []
        if "order_status_history" in tables:
            history = [
                _decode_fields(row, "detail_json")
                for row in connection.execute(
                    "SELECT * FROM order_status_history WHERE chain_id=? ORDER BY id",
                    (chain_id,),
                ).fetchall()
            ]
        fills: list[dict[str, Any]] = []
        if "fills" in tables:
            fills = [
                _decode_fields(row, "payload_json")
                for row in connection.execute(
                    "SELECT * FROM fills WHERE chain_id=? ORDER BY filled_at, fill_id",
                    (chain_id,),
                ).fetchall()
            ]
        for fill in fills:
            fill_count += 1
            amount = _decimal(fill.get("quantity")) * _decimal(fill.get("price")) * Decimal("100")
            cash_flow += amount if str(fill.get("side", "")).lower() == "sell" else -amount
        chain["attempts"] = attempts
        chain["status_history"] = history
        chain["fills"] = fills
        chains.append(chain)
    return {
        "chains": chains,
        "chain_count": len(chains),
        "fill_count": fill_count,
        "option_cash_flow_ex_fees": _decimal_string(cash_flow),
        "focus_run_id": run_id,
    }


def _portfolio_summary(
    connection: sqlite3.Connection, tables: set[str], run_id: str | None
) -> dict[str, Any]:
    position_history: list[dict[str, Any]] = []
    if "position_observations" in tables:
        rows = connection.execute(
            """
            SELECT * FROM position_observations
            ORDER BY observed_at DESC
            """
        ).fetchall()
        for row in rows:
            item = _decode_fields(row, "payload_json")
            item["is_flat"] = bool(item["is_flat"])
            position_history.append(item)

    equity_history: list[dict[str, Any]] = []
    if "equity_observations" in tables:
        rows = connection.execute(
            """
            SELECT * FROM equity_observations
            ORDER BY observed_at ASC
            """
        ).fetchall()
        equity_history = [_decode_fields(row, "payload_json") for row in rows]

    if not equity_history and "account_snapshots" in tables:
        rows = connection.execute(
            """
            SELECT observed_at, equity, buying_power
            FROM account_snapshots WHERE equity IS NOT NULL
            ORDER BY observed_at ASC
            """
        ).fetchall()
        equity_history = [dict(row) for row in rows]

    first_equity = equity_history[0].get("equity") if equity_history else None
    latest_equity = equity_history[-1].get("equity") if equity_history else None
    observed_change = None
    if first_equity is not None and latest_equity is not None:
        observed_change = _decimal_string(
            _decimal(latest_equity) - _decimal(first_equity)
        )
    latest_observed_at = (
        equity_history[-1].get("observed_at") if equity_history else None
    )
    return {
        "latest_position_observation": position_history[0] if position_history else None,
        "position_history": position_history,
        "equity": {
            "first": _as_string(first_equity),
            "latest": _as_string(latest_equity),
            "observed_change": observed_change,
            "latest_observed_at": latest_observed_at,
            "history": equity_history,
        },
        "focus_run_id": run_id,
    }


def _kill_switch_summary(
    connection: sqlite3.Connection, tables: set[str]
) -> dict[str, Any]:
    if "runtime_controls" not in tables:
        return {
            "known": False,
            "enabled": True,
            "reason": "runtime controls are unavailable",
            "version": None,
            "recent_events": [],
        }
    row = connection.execute(
        "SELECT * FROM runtime_controls WHERE singleton_id=1"
    ).fetchone()
    if row is None:
        return {
            "known": False,
            "enabled": True,
            "reason": "runtime controls are missing",
            "version": None,
            "recent_events": [],
        }
    result = dict(row)
    result["known"] = True
    result["enabled"] = bool(result.pop("kill_switch_enabled"))
    events: list[dict[str, Any]] = []
    if "control_events" in tables:
        event_rows = connection.execute(
            "SELECT * FROM control_events ORDER BY id DESC"
        ).fetchall()
        for event_row in event_rows:
            event = _decode_fields(event_row, "detail_json")
            event["enabled"] = bool(event["enabled"])
            events.append(event)
    result["recent_events"] = events
    return result


def _entry_permission_summary(
    connection: sqlite3.Connection,
    tables: set[str],
    *,
    environment: str,
    account_id: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Return credential-free effective state for the latest one-shot entry."""

    missing = {
        "known": False,
        "active": False,
        "state": "MISSING",
        "strategy_date": None,
        "expires_at": None,
    }
    if "entry_authorizations" not in tables or not account_id:
        return missing
    row = connection.execute(
        """
        SELECT state, strategy_date, expires_at, requested_by, reason, armed_at,
               consumed_at, consumed_run_id, consumed_intent_id,
               consumed_chain_id, consumed_attempt_id, revoked_at, revoked_by,
               revoke_reason
        FROM entry_authorizations
        WHERE environment=? AND account_id=?
        ORDER BY armed_at DESC, authorization_id DESC
        LIMIT 1
        """,
        (environment, account_id),
    ).fetchone()
    if row is None:
        return missing
    result = dict(row)
    persisted_state = str(result.get("state") or "INVALID").upper()
    effective_state = persisted_state
    active = False
    if persisted_state == "ARMED":
        try:
            armed_at = datetime.fromisoformat(
                str(result["armed_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
            expires_at = datetime.fromisoformat(
                str(result["expires_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            effective_state = "INVALID"
        else:
            if now < armed_at:
                effective_state = "NOT_ACTIVE"
            elif now >= expires_at:
                effective_state = "EXPIRED"
            else:
                active = True
    result.update(
        {
            "known": True,
            "active": active,
            "persisted_state": persisted_state,
            "state": effective_state,
        }
    )
    return result


def _entry_permission_history(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    if "entry_authorizations" not in tables:
        return []
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT state, strategy_date, expires_at, reason, armed_at,
                   consumed_at, revoked_at, revoke_reason
            FROM entry_authorizations
            ORDER BY armed_at DESC
            """
        ).fetchall()
    ]


def _decode_fields(row: sqlite3.Row, *fields: str) -> dict[str, Any]:
    result = dict(row)
    for field in fields:
        raw = result.pop(field, None)
        result[field.removesuffix("_json")] = _decode_json(raw, None)
    return result


def _decode_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return {"_error": "invalid persisted JSON"}


def _sanitize_for_report(value: Any) -> Any:
    return redact(value)


def _result_summary(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, Mapping):
        return {
            "type": "object",
            "keys": sorted(str(key) for key in value.keys())[:30],
            "key_count": len(value),
        }
    if isinstance(value, list):
        return {"type": "array", "item_count": len(value)}
    return {"type": type(value).__name__}


def _latest_reason(transitions: list[dict[str, Any]]) -> str | None:
    for transition in reversed(transitions):
        if transition.get("to_state") == "NO_TRADE":
            return _as_string(transition.get("reason_code"))
    return None


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def _decimal_string(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _digest(report: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return result.astimezone(UTC)


def _as_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) is not None:
            return payload[key]
    return "n/a"


def _md(value: Any) -> str:
    if value is None:
        return "none"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _money(value: Any, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    number = _decimal(value)
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}${number:,.2f}"


def _markdown_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    materialized = [[_md(value) for value in row] for row in rows]
    if not materialized:
        return ["No records."]
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in materialized),
    ]
