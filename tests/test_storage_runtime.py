from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thetatrap.storage import StorageInvariantError, Store


NOW = datetime(2026, 9, 1, 18, 50, tzinfo=UTC)


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "runtime.sqlite3")
    store.initialize()
    return store


def create_run(store: Store, run_id: str = "run-1", strategy_date: str = "2026-09-01") -> None:
    store.create_strategy_run(
        run_id,
        environment="development",
        strategy_date=strategy_date,
        strategy_version="1.1",
        config_hash="config-hash",
        context={"source": "test"},
    )


def test_runtime_schema_and_strategy_transitions_are_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.initialize()
    assert store.get_metadata("schema_version") == "2"
    assert store.get_metadata("runtime_schema_version") == "5"

    first = store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="config-hash",
        context={"b": 2, "a": 1},
    )
    repeated = store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="config-hash",
        context={"a": 1, "b": 2},
    )
    assert first["state"] == repeated["state"] == "DISCOVERING"

    with pytest.raises(StorageInvariantError, match="canonical strategy run"):
        store.create_strategy_run(
            "another-run",
            environment="development",
            strategy_date="2026-09-01",
            strategy_version="1.1",
            config_hash="config-hash",
        )

    screening = store.transition_strategy_run(
        "run-1", "SCREENING", "WINDOW_OPEN", {"clock": "open"}
    )
    assert screening["state"] == "SCREENING"
    repeated_transition = store.transition_strategy_run(
        "run-1", "SCREENING", "WINDOW_OPEN", {"clock": "open"}
    )
    assert repeated_transition["state"] == "SCREENING"
    resumed = store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date="2026-09-01",
        strategy_version="1.1",
        config_hash="config-hash",
        context={"a": 1, "b": 2},
    )
    assert resumed["state"] == "SCREENING"
    with pytest.raises(StorageInvariantError, match="invalid strategy transition"):
        store.transition_strategy_run("run-1", "SUBMITTING", "SKIP_POLICY")

    transitions = store.list_strategy_transitions("run-1")
    assert [item["to_state"] for item in transitions] == ["DISCOVERING", "SCREENING"]
    assert transitions[-1]["evidence"] == {"clock": "open"}
    assert store.find_strategy_run(
        environment="development", strategy_date="2026-09-01", strategy_version="1.1"
    )["run_id"] == "run-1"
    assert store.find_active_strategy_run(environment="development")["run_id"] == "run-1"


def test_snapshots_candidates_and_gate_results_are_immutable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    create_run(store)

    snapshot = store.record_collection_snapshot(
        "snapshot-1",
        run_id="run-1",
        symbol="PANW",
        collection_type="option_chain",
        observed_at=NOW,
        payload={"quotes": {"ask": "1.20", "bid": "1.00"}},
    )
    repeated = store.record_collection_snapshot(
        "snapshot-1",
        run_id="run-1",
        symbol="PANW",
        collection_type="option_chain",
        observed_at=NOW,
        payload={"quotes": {"bid": "1.00", "ask": "1.20"}},
    )
    assert snapshot["payload_hash"] == repeated["payload_hash"]
    with pytest.raises(StorageInvariantError, match="collection snapshot"):
        store.record_collection_snapshot(
            "snapshot-1",
            run_id="run-1",
            symbol="PANW",
            collection_type="option_chain",
            observed_at=NOW,
            payload={"quotes": []},
        )

    candidate = store.record_candidate(
        "candidate-1",
        run_id="run-1",
        snapshot_id="snapshot-1",
        symbol="PANW",
        candidate_rank=1,
        eligible=True,
        payload={"max_loss": "425.00"},
    )
    assert candidate["eligible"] is True
    assert store.list_candidates("run-1")[0]["candidate_id"] == "candidate-1"
    with pytest.raises(StorageInvariantError, match="candidate identity or rank"):
        store.record_candidate(
            "candidate-2",
            run_id="run-1",
            snapshot_id="snapshot-1",
            symbol="PANW",
            candidate_rank=1,
            eligible=True,
            payload={},
        )

    gate = store.record_gate_result(
        "candidate-1",
        "screening",
        "MAX_LOSS",
        passed=True,
        detail={"value": "425.00", "limit": "500.00"},
        evaluated_at=NOW,
    )
    repeated_gate = store.record_gate_result(
        "candidate-1",
        "screening",
        "MAX_LOSS",
        passed=True,
        detail={"limit": "500.00", "value": "425.00"},
        evaluated_at=NOW + timedelta(seconds=1),
    )
    assert gate["id"] == repeated_gate["id"]
    with pytest.raises(StorageInvariantError, match="gate result"):
        store.record_gate_result(
            "candidate-1", "screening", "MAX_LOSS", passed=False
        )


def test_agent_run_and_tool_trace_are_durable_and_ordered(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    create_run(store)
    started = store.start_agent_run(
        "agent-1",
        run_id="run-1",
        model="Qwen/Qwen3-Coder-Next",
        prompt_hash="prompt-hash",
        config_hash="agent-config-hash",
        started_at=NOW,
    )
    assert started["status"] == "STARTED"

    call = store.record_agent_tool_call(
        "agent-1",
        0,
        principal="qwen",
        tool_name="get_clock",
        arguments={},
        result={"data": {"is_open": True}},
        status="ok",
        duration_ms=12,
        is_official_mcp=True,
        called_at=NOW,
    )
    assert call["is_official_mcp"] is True
    assert store.list_agent_tool_calls("agent-1")[0]["result"]["data"]["is_open"] is True
    with pytest.raises(StorageInvariantError, match="agent tool trace"):
        store.record_agent_tool_call(
            "agent-1",
            0,
            principal="qwen",
            tool_name="get_orders",
            arguments={},
            result={},
            status="ok",
            duration_ms=12,
            is_official_mcp=True,
        )

    finished = store.finish_agent_run(
        "agent-1", "COMPLETED", result={"decision": "ALLOW"}, ended_at=NOW
    )
    assert finished["result"] == {"decision": "ALLOW"}
    repeated = store.finish_agent_run(
        "agent-1", "COMPLETED", result={"decision": "ALLOW"}, ended_at=NOW
    )
    assert repeated["status"] == "COMPLETED"
    resumed = store.start_agent_run(
        "agent-1",
        run_id="run-1",
        model="Qwen/Qwen3-Coder-Next",
        prompt_hash="prompt-hash",
        config_hash="agent-config-hash",
    )
    assert resumed["status"] == "COMPLETED"
    with pytest.raises(StorageInvariantError, match="finished agent run"):
        store.finish_agent_run("agent-1", "VETOED", veto_reason="TRADING_HALT")


def test_standalone_agent_smoke_persists_hashes_not_broker_payloads(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    started = store.start_agent_smoke(
        "smoke-1",
        environment="development",
        account_suffix="…ef9876",
        model="Qwen/Qwen3-Coder-Next",
        prompt_hash="a" * 64,
        config_hash="b" * 64,
        started_at=NOW,
    )
    assert started["status"] == "STARTED"
    trace = store.record_agent_smoke_tool(
        "smoke-1",
        0,
        turn=1,
        tool_name="get_account_info",
        arguments_hash="c" * 64,
        result_hash="d" * 64,
        status="ok",
        duration_ms=4,
        called_at=NOW,
    )
    assert trace["arguments_hash"] == "c" * 64
    assert set(store.list_agent_smoke_tools("smoke-1")[0]) >= {
        "arguments_hash",
        "result_hash",
    }
    finished = store.finish_agent_smoke(
        "smoke-1",
        "COMPLETED",
        result={"outcome": "PASS", "account_suffix": "…ef9876"},
        ended_at=NOW,
    )
    assert finished["result"] == {
        "outcome": "PASS",
        "account_suffix": "…ef9876",
    }

    with store.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_smoke_trace)")
        }
        assert "arguments_json" not in columns
        assert "result_json" not in columns
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE agent_smoke_trace SET tool_name='get_orders' WHERE id=?",
                (trace["id"],),
            )


def test_order_intent_chain_attempt_status_and_fill_invariants(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    create_run(store)
    payload = {
        "qty": "1",
        "type": "limit",
        "limit_price": "-0.75",
        "legs": [
            {"symbol": "PANW260904P00100000", "side": "buy"},
            {"symbol": "PANW260904P00105000", "side": "sell"},
            {"symbol": "PANW260904C00150000", "side": "sell"},
            {"symbol": "PANW260904C00155000", "side": "buy"},
        ],
    }
    intent = store.record_order_intent(
        "intent-1",
        run_id="run-1",
        purpose="entry",
        client_order_id="tt-entry-1",
        payload=payload,
    )
    assert len(intent["payload_hash"]) == 64
    assert store.order_intent_matches("intent-1", payload)
    assert not store.order_intent_matches("intent-1", {**payload, "qty": "2"})
    with pytest.raises(StorageInvariantError, match="order intent"):
        store.record_order_intent(
            "intent-2",
            run_id="run-1",
            purpose="entry",
            client_order_id="tt-entry-1",
            payload=payload,
        )
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE order_intents SET payload_hash='changed' WHERE intent_id='intent-1'"
            )

    chain = store.create_order_chain(
        "chain-1", run_id="run-1", intent_id="intent-1", purpose="entry"
    )
    assert chain["state"] == "PLANNED"
    store.transition_order_chain("chain-1", "SUBMITTING")
    store.transition_order_chain("chain-1", "UNKNOWN", detail={"timeout": True})
    pending = store.transition_order_chain("chain-1", "PENDING")
    assert pending["state"] == "PENDING"
    resumed_chain = store.create_order_chain(
        "chain-1", run_id="run-1", intent_id="intent-1", purpose="entry"
    )
    assert resumed_chain["state"] == "PENDING"
    with pytest.raises(StorageInvariantError, match="invalid order chain transition"):
        store.transition_order_chain("chain-1", "PLANNED")

    attempt = store.record_order_attempt(
        "attempt-1",
        chain_id="chain-1",
        sequence=0,
        client_order_id="tt-entry-1",
        request=payload,
    )
    assert attempt["request_hash"] == intent["payload_hash"]
    bound = store.bind_broker_order_id("attempt-1", "broker-order-1")
    assert bound["broker_order_id"] == "broker-order-1"
    resumed_attempt = store.record_order_attempt(
        "attempt-1",
        chain_id="chain-1",
        sequence=0,
        client_order_id="tt-entry-1",
        request=payload,
    )
    assert resumed_attempt["broker_order_id"] == "broker-order-1"
    with pytest.raises(StorageInvariantError, match="immutable once bound"):
        store.bind_broker_order_id("attempt-1", "broker-order-2")

    status = store.record_order_status(
        "chain-1",
        "accepted",
        attempt_id="attempt-1",
        detail={"filled_qty": "0"},
        observed_at=NOW,
    )
    assert status["detail"] == {"filled_qty": "0"}
    fill = store.record_fill(
        "fill-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        broker_order_id="broker-order-1",
        symbol="PANW260904P00100000",
        side="buy",
        quantity="1",
        price="0.10",
        filled_at=NOW,
        payload={"activity_type": "FILL"},
    )
    assert fill["payload"] == {"activity_type": "FILL"}
    repeated_fill = store.record_fill(
        "fill-1",
        chain_id="chain-1",
        attempt_id="attempt-1",
        broker_order_id="broker-order-1",
        symbol="PANW260904P00100000",
        side="buy",
        quantity="1",
        price="0.10",
        filled_at=NOW,
        payload={"activity_type": "FILL"},
    )
    assert repeated_fill["fill_id"] == "fill-1"
    assert len(store.list_order_status_history("chain-1")) == 5


def test_observations_kill_switch_and_submission_interlock(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    create_run(store)
    store.transition_strategy_run("run-1", "SCREENING", "WINDOW_OPEN")

    positions = store.record_position_observation(
        "positions-1",
        run_id="run-1",
        observed_at=NOW,
        positions=[],
        is_flat=True,
    )
    assert positions["is_flat"] is True
    equity = store.record_equity_observation(
        "equity-1",
        run_id="run-1",
        observed_at=NOW,
        equity="100000.00",
        buying_power="100000.00",
        payload={"status": "ACTIVE"},
    )
    assert equity["equity"] == "100000.00"

    control = store.activate_kill_switch(
        "operator requested",
        "dashboard",
        run_id="run-1",
        evidence={"button": "kill"},
        activated_at=NOW,
    )
    assert control["kill_switch_enabled"] is True
    assert store.get_strategy_run("run-1")["state"] == "RISK_OFF"  # type: ignore[index]
    with pytest.raises(StorageInvariantError, match="blocks entry order intent"):
        store.record_order_intent(
            "blocked-intent",
            run_id="run-1",
            purpose="entry",
            client_order_id="blocked-entry",
            payload={"qty": "1"},
        )

    create_run(store, "run-2", "2026-09-02")
    store.transition_strategy_run("run-2", "SCREENING", "WINDOW_OPEN")
    store.transition_strategy_run("run-2", "AI_REVIEW", "CANDIDATE_READY")
    store.transition_strategy_run("run-2", "POLICY_CHECK", "MODEL_ALLOWED")
    with pytest.raises(StorageInvariantError, match="blocks transition to SUBMITTING"):
        store.transition_strategy_run("run-2", "SUBMITTING", "POLICY_PASSED")

    exit_intent = store.record_order_intent(
        "exit-intent",
        run_id="run-1",
        purpose="exit",
        client_order_id="tt-exit-1",
        payload={"qty": "1"},
    )
    assert exit_intent["purpose"] == "exit"
    cleared = store.clear_kill_switch(
        "risk reviewed",
        "operator",
        expected_version=control["version"],
        cleared_at=NOW + timedelta(minutes=1),
    )
    assert cleared["kill_switch_enabled"] is False
    with pytest.raises(StorageInvariantError, match="version changed"):
        store.clear_kill_switch(
            "stale request", "operator", expected_version=control["version"]
        )


def test_ttl_single_flight_lease_has_owner_and_expiry_guards(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.acquire_lease("worker-cycle", "worker-a", 60, now=NOW)
    assert not store.acquire_lease(
        "worker-cycle", "worker-b", 60, now=NOW + timedelta(seconds=30)
    )
    assert not store.renew_lease(
        "worker-cycle", "worker-b", 60, now=NOW + timedelta(seconds=30)
    )
    assert store.renew_lease(
        "worker-cycle", "worker-a", 60, now=NOW + timedelta(seconds=30)
    )
    assert store.acquire_lease(
        "worker-cycle", "worker-b", 60, now=NOW + timedelta(seconds=91)
    )
    lease = store.get_lease("worker-cycle")
    assert lease is not None and lease["owner_id"] == "worker-b"
    assert not store.release_lease("worker-cycle", "worker-a")
    assert store.release_lease("worker-cycle", "worker-b")
    assert store.get_lease("worker-cycle") is None
