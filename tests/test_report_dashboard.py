from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thetatrap.dashboard import CLEAR_CONFIRMATION, update_kill_switch
from thetatrap.events import load_events
from thetatrap.report import (
    ReportUnavailable,
    build_operational_report,
    generate_report,
    render_markdown,
)
from thetatrap.storage import Store


NOW = datetime(2026, 9, 1, 18, 50, tzinfo=UTC)


def populated_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "runtime.sqlite3")
    store.initialize()
    store.upsert_events(load_events())
    store.bind_identity("development", "account-123456789", "account-123456789")
    store.record_heartbeat(
        status="healthy",
        environment="development",
        account_suffix="…456789",
        mcp_schema_hash="schema-hash",
        market_is_open=True,
        detail={"options_level": 3, "api_key": "must-not-appear"},
    )
    store.start_mcp_session(
        "mcp-1",
        package_version="2.3.0",
        server_name="alpaca",
        tool_count=54,
        required_schema_hash="required-hash",
    )
    store.record_mcp_call(
        "mcp-1",
        "system",
        "get_clock",
        {},
        {"data": {"is_open": True}},
        "ok",
        12,
    )
    store.finish_mcp_session("mcp-1", "closed")

    store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="strategy-config-hash",
        context={"event": "PANW"},
    )
    store.transition_strategy_run("run-1", "SCREENING", "ENTRY_WINDOW_OPEN")
    store.record_collection_snapshot(
        "snapshot-1",
        run_id="run-1",
        symbol="PANW",
        collection_type="option_chain",
        observed_at=NOW,
        payload={"feed": "indicative"},
    )
    store.record_candidate(
        "candidate-1",
        run_id="run-1",
        snapshot_id="snapshot-1",
        symbol="PANW",
        candidate_rank=1,
        eligible=True,
        payload={
            "iv_ratio": "1.24",
            "expected_move": "14.20",
            "proposed_credit": "0.70",
            "maximum_loss": "430.00",
            "legs": [
                {"role": "long_put", "symbol": "PANW260904P00100000"},
                {"role": "short_put", "symbol": "PANW260904P00105000"},
                {"role": "short_call", "symbol": "PANW260904C00150000"},
                {"role": "long_call", "symbol": "PANW260904C00155000"},
            ],
        },
    )
    store.record_gate_result(
        "candidate-1",
        "screen-1",
        "IV_RATIO",
        passed=True,
        detail={"value": "1.24"},
        evaluated_at=NOW,
    )
    store.record_gate_result(
        "candidate-1",
        "screen-2",
        "MAX_LOSS",
        passed=True,
        detail={"value": "430.00"},
        evaluated_at=NOW + timedelta(seconds=1),
    )

    store.record_collection_snapshot(
        "snapshot-2",
        run_id="run-1",
        symbol="MDB",
        collection_type="strategy_market_bundle",
        observed_at=NOW + timedelta(seconds=2),
        payload={"feed": "indicative"},
    )
    store.record_candidate(
        "candidate-2",
        run_id="run-1",
        snapshot_id="snapshot-2",
        symbol="MDB",
        candidate_rank=None,
        eligible=False,
        payload={
            "symbol": "MDB",
            "candidate": None,
            "failures": [
                {
                    "code": "IV_RATIO_LOW",
                    "detail": "front/back ATM IV ratio 1.02 is below 1.10",
                }
            ],
        },
    )
    store.record_gate_result(
        "candidate-2",
        "screen-mdb-1",
        "IV_RATIO_LOW",
        passed=False,
        reason_code="IV_RATIO_LOW",
        detail={"value": "1.02", "minimum": "1.10"},
        evaluated_at=NOW + timedelta(seconds=2),
    )

    store.start_advisory_run(
        "advisory-1",
        run_id="run-1",
        candidate_id="candidate-2",
        model="Qwen/Qwen3-Coder-Next",
        prompt_hash="a" * 64,
        config_hash="b" * 64,
        started_at=NOW + timedelta(seconds=3),
    )
    store.record_advisory_tool_call(
        "advisory-1",
        0,
        turn=1,
        tool_name="get_clock",
        arguments_hash="c" * 64,
        result_hash="d" * 64,
        status="ok",
        duration_ms=9,
        called_at=NOW + timedelta(seconds=3),
    )
    store.finish_advisory_run(
        "advisory-1",
        "COMPLETED",
        result={
            "assessment": "DETERMINISTIC_REJECTION_CONFIRMED",
            "summary": "The IV-ratio gate remains binding.",
            "evidence": ["Observed ratio 1.02 is below frozen minimum 1.10."],
            "non_authorizing": True,
        },
        ended_at=NOW + timedelta(seconds=4),
    )
    store.record_gate_result(
        "candidate-1",
        "screen-2",
        "QUOTE_FRESHNESS",
        passed=True,
        detail={"age_seconds": 1},
        evaluated_at=NOW + timedelta(seconds=1),
    )

    store.transition_strategy_run("run-1", "AI_REVIEW", "CANDIDATE_READY")
    store.start_agent_run(
        "agent-1",
        run_id="run-1",
        candidate_id="candidate-1",
        model="Qwen/Qwen3-Coder-Next",
        prompt_hash="prompt-hash",
        config_hash="agent-config-hash",
        started_at=NOW,
    )
    store.record_agent_tool_call(
        "agent-1",
        0,
        principal="qwen",
        tool_name="get_clock",
        arguments={"api_key": "must-not-appear"},
        result={"data": {"is_open": True, "account_id": "private-account"}},
        status="ok",
        duration_ms=15,
        is_official_mcp=True,
        called_at=NOW,
    )
    store.finish_agent_run(
        "agent-1",
        "COMPLETED",
        result={"decision": "ALLOW", "explanation": "No finite veto found."},
        ended_at=NOW + timedelta(seconds=2),
    )
    store.transition_strategy_run("run-1", "POLICY_CHECK", "MODEL_ALLOWED")

    order_payload = {
        "qty": "1",
        "type": "limit",
        "limit_price": "-0.70",
        "time_in_force": "day",
        "order_class": "mleg",
        "client_order_id": "tt-entry-pan-1",
        "legs": [
            {"symbol": "PANW260904P00100000", "side": "buy"},
            {"symbol": "PANW260904P00105000", "side": "sell"},
            {"symbol": "PANW260904C00150000", "side": "sell"},
            {"symbol": "PANW260904C00155000", "side": "buy"},
        ],
    }
    store.record_order_intent(
        "intent-1",
        run_id="run-1",
        candidate_id="candidate-1",
        purpose="entry",
        client_order_id="tt-entry-pan-1",
        payload=order_payload,
    )
    store.create_order_chain(
        "chain-1", run_id="run-1", intent_id="intent-1", purpose="entry"
    )
    store.record_order_attempt(
        "attempt-1",
        chain_id="chain-1",
        sequence=0,
        client_order_id="tt-entry-pan-1",
        request=order_payload,
        broker_order_id="broker-1",
    )
    store.transition_strategy_run("run-1", "SUBMITTING", "POLICY_PASSED")
    store.transition_order_chain("chain-1", "SUBMITTING", attempt_id="attempt-1")
    store.transition_strategy_run("run-1", "ORDER_PENDING", "BROKER_ACCEPTED")
    store.transition_order_chain("chain-1", "PENDING", attempt_id="attempt-1")
    store.record_order_status(
        "chain-1",
        "accepted",
        attempt_id="attempt-1",
        detail={"filled_qty": "0"},
        observed_at=NOW + timedelta(seconds=3),
    )
    fill_rows = [
        ("fill-1", "PANW260904P00100000", "buy", "0.10"),
        ("fill-2", "PANW260904P00105000", "sell", "0.40"),
        ("fill-3", "PANW260904C00150000", "sell", "0.50"),
        ("fill-4", "PANW260904C00155000", "buy", "0.10"),
    ]
    for fill_id, symbol, side, price in fill_rows:
        store.record_fill(
            fill_id,
            chain_id="chain-1",
            attempt_id="attempt-1",
            broker_order_id="broker-1",
            symbol=symbol,
            side=side,
            quantity="1",
            price=price,
            filled_at=NOW + timedelta(seconds=4),
            payload={"activity_type": "FILL"},
        )
    store.transition_order_chain("chain-1", "FILLED", attempt_id="attempt-1")
    store.transition_strategy_run("run-1", "POSITION_OPEN", "ENTRY_FILLED")
    store.record_position_observation(
        "positions-1",
        run_id="run-1",
        observed_at=NOW + timedelta(seconds=5),
        positions=[{"symbol": "PANW260904P00100000", "qty": "1"}],
        is_flat=False,
    )
    store.record_equity_observation(
        "equity-1",
        run_id="run-1",
        observed_at=NOW,
        equity="100000.00",
        buying_power="100000.00",
    )
    store.record_equity_observation(
        "equity-2",
        run_id="run-1",
        observed_at=NOW + timedelta(minutes=5),
        equity="100075.00",
        buying_power="99500.00",
    )
    return store


def test_report_reconciles_strategy_agent_orders_positions_and_equity(tmp_path: Path) -> None:
    store = populated_store(tmp_path)
    report = build_operational_report(
        store.path,
        execution_enabled=False,
        read_only=True,
        generated_at=NOW,
    )

    assert report["mode"]["banner"] == "PAPER · TRADING DISARMED"
    assert report["report_schema_version"] == "2"
    assert report["mode"]["data_feed"] == "BASIC INDICATIVE"
    assert report["data_profile"]["profile_id"] == "alpaca_basic_iex_indicative_v1"
    assert report["identity"]["account_suffix"] == "…456789"
    assert report["mcp"]["latest_session"]["status"] == "closed"
    assert report["mcp"]["operational_status"] == "ready"
    assert report["strategy"]["current_run"]["state"] == "POSITION_OPEN"
    assert report["candidate"]["selected"]["symbol"] == "PANW"
    assert [
        gate["gate_name"] for gate in report["candidate"]["latest_gate_outcomes"]
    ] == ["MAX_LOSS", "QUOTE_FRESHNESS"]
    assert {item["symbol"] for item in report["candidate"]["history"]} == {
        "PANW",
        "MDB",
    }
    rejected = next(
        item for item in report["candidate"]["history"] if item["symbol"] == "MDB"
    )
    assert rejected["failed_gate_names"] == ["IV_RATIO_LOW"]
    assert len(report["candidate"]["scan_matrix"]) == 9
    matrix = {item["symbol"]: item for item in report["candidate"]["scan_matrix"]}
    assert matrix["PANW"]["latest_result"] == "ELIGIBLE"
    assert matrix["MDB"]["latest_result"] == "REJECTED"
    assert matrix["DELL"]["latest_result"] == "EXCLUDED"
    assert report["agent"]["latest_run"]["result"]["decision"] == "ALLOW"
    assert report["agent"]["tool_trace"][0]["is_official_mcp"] is True
    assert report["agent"]["tool_trace"][0]["result_summary"] == {
        "type": "object",
        "keys": ["data"],
        "key_count": 1,
    }
    assert len(report["agent"]["reviews"]) == 1
    assert report["agent"]["reviews"][0]["symbol"] == "PANW"
    assert report["agent"]["advisories"][0]["mode"] == (
        "READ_ONLY_REJECTED_CANDIDATE_ADVISORY"
    )
    assert report["agent"]["advisories"][0]["symbol"] == "MDB"
    assert report["agent"]["advisories"][0]["tool_trace"][0]["tool_name"] == (
        "get_clock"
    )
    assert report["mcp"]["timeline"][0]["tool_name"] == "get_clock"
    assert report["strategy"]["transition_history"]
    assert report["orders"]["chains"][0]["state"] == "FILLED"
    assert report["orders"]["fill_count"] == 4
    assert report["orders"]["option_cash_flow_ex_fees"] == "70.00"
    assert report["portfolio"]["latest_position_observation"]["is_flat"] is False
    assert report["portfolio"]["equity"]["observed_change"] == "75.00"
    assert report["safety"]["maximum_defined_loss"] == "500.00"
    assert report["safety"]["read_only_viewer"] is True

    serialized = json.dumps(report)
    assert "must-not-appear" not in serialized
    assert "private-account" not in serialized
    assert "[REDACTED]" in serialized
    assert len(report["report_digest"]) == 64


def test_markdown_and_file_exports_are_judge_readable(tmp_path: Path) -> None:
    store = populated_store(tmp_path)
    report = build_operational_report(store.path, generated_at=NOW)
    markdown = render_markdown(report)
    assert "PAPER · TRADING DISARMED" in markdown
    assert "Qwen/Qwen3-Coder-Next" in markdown
    assert "Observed-window change: `+$75.00`" in markdown
    assert "Paper fills are simulated" in markdown

    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    json_report = generate_report(store.path, json_path, generated_at=NOW)
    md_report = generate_report(store.path, md_path, generated_at=NOW)
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_digest"] == json_report[
        "report_digest"
    ]
    assert md_path.read_text(encoding="utf-8").startswith("# ThetaTrap final run report")
    assert md_report["report_digest"] == json_report["report_digest"]


def test_report_fails_cleanly_for_missing_database(tmp_path: Path) -> None:
    with pytest.raises(ReportUnavailable, match="does not exist"):
        build_operational_report(tmp_path / "missing.sqlite3")


def test_report_requires_an_active_one_shot_permission_to_show_entry_armed(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "one-shot.sqlite3")
    store.initialize()
    store.bind_identity("development", "account-123456789", "account-123456789")
    store.arm_entry_authorization(
        "entry-auth-report",
        environment="development",
        account_id="account-123456789",
        strategy_date="2026-09-01",
        expires_at=NOW + timedelta(hours=1),
        requested_by="test_operator",
        reason="report state test",
        armed_at=NOW - timedelta(minutes=1),
    )

    armed = build_operational_report(
        store.path,
        execution_enabled=True,
        read_only=False,
        environment="development",
        generated_at=NOW,
    )
    assert armed["one_shot_entry"]["state"] == "ARMED"
    assert armed["one_shot_entry"]["active"] is True
    assert armed["mode"]["trading_state"] == "ARMED"

    expired = build_operational_report(
        store.path,
        execution_enabled=True,
        read_only=False,
        environment="development",
        generated_at=NOW + timedelta(hours=2),
    )
    assert expired["one_shot_entry"]["state"] == "EXPIRED"
    assert expired["one_shot_entry"]["active"] is False
    assert expired["mode"]["trading_state"] == "DISARMED"


def test_dashboard_kill_switch_activation_and_guarded_clear(tmp_path: Path) -> None:
    store = Store(tmp_path / "control.sqlite3")
    store.initialize()
    store.create_strategy_run(
        "run-control",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="hash",
    )
    store.transition_strategy_run("run-control", "SCREENING", "WINDOW_OPEN")

    active = update_kill_switch(
        store.path,
        enabled=True,
        reason="operator emergency stop",
    )
    assert active["kill_switch_enabled"] is True
    assert store.get_strategy_run("run-control")["state"] == "RISK_OFF"  # type: ignore[index]

    with pytest.raises(ValueError, match="CLEAR KILL SWITCH"):
        update_kill_switch(
            store.path,
            enabled=False,
            reason="risk reviewed",
            expected_version=active["version"],
            clear_confirmation="clear",
        )

    cleared = update_kill_switch(
        store.path,
        enabled=False,
        reason="risk reviewed",
        expected_version=active["version"],
        clear_confirmation=CLEAR_CONFIRMATION,
    )
    assert cleared["kill_switch_enabled"] is False
    assert cleared["version"] == active["version"] + 1
