from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from thetatrap.replay import run_replay_suite


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "replay" / "expected_scenarios.json"


def _by_name(report: dict) -> dict[str, dict]:
    return {scenario["name"]: scenario for scenario in report["scenarios"]}


def test_replay_covers_exact_acceptance_scenarios_without_external_mutation() -> None:
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    report = run_replay_suite()

    assert report["passed"] is True
    assert report["storage_mode"] == "in_memory"
    assert report["simulation_label"] == expected["simulation_label"]
    assert report["scenario_count"] == 5
    assert [item["name"] for item in report["scenarios"]] == expected[
        "scenario_names"
    ]
    assert report["external_broker_mutation_calls"] == expected[
        "expected_external_broker_mutation_calls"
    ]
    assert all(item["passed"] for item in report["scenarios"])
    assert all(
        item["simulation_label"] == expected["simulation_label"]
        and item["external_broker_mutation_calls"] == 0
        for item in report["scenarios"]
    )


def test_replay_evidence_proves_each_required_path() -> None:
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    scenarios = _by_name(run_replay_suite())

    eligible = scenarios["eligible_candidate"]["details"]
    assert eligible["eligible"] is True
    assert eligible["proposed_credit"] == expected["expected_entry_credit"]
    assert eligible["maximum_loss"] == expected["expected_maximum_loss"]

    rejection = scenarios["deterministic_rejection"]["details"]
    assert rejection["eligible"] is False
    assert rejection["failure_codes"] == [expected["expected_rejection_code"]]
    assert rejection["repeat_evaluation_identical"] is True

    veto = scenarios["model_veto"]["details"]
    assert veto["candidate_was_eligible"] is True
    assert veto["decision"]["reason_code"] == expected["expected_veto_code"]
    assert veto["decision"]["order_authorized"] is False
    assert veto["model_network_calls"] == 0

    timeout = scenarios["ambiguous_timeout_reconciliation"]["details"]
    assert timeout["timeout_was_post_dispatch"] is True
    assert timeout["deterministic_client_order_id"] == timeout[
        "reconciled_client_order_id"
    ]
    assert timeout["simulated_dispatch_attempts"] == 1
    assert timeout["orders_with_client_order_id"] == 1
    assert timeout["duplicate_submissions"] == 0
    assert timeout["retry_suppressed_after_reconciliation"] is True

    exit_result = scenarios["opposite_side_exit_to_flat"]["details"]
    assert exit_result["entry_leg_count"] == exit_result["exit_leg_count"] == 4
    assert all(leg["opposite_side"] for leg in exit_result["legs"])
    assert all(leg["final_quantity"] == "0" for leg in exit_result["legs"])
    assert exit_result["position_state"] == expected["expected_exit_position_state"]


def test_replay_is_deterministic() -> None:
    first = run_replay_suite()
    second = run_replay_suite(":memory:")

    assert first["result_digest"] == second["result_digest"]
    assert first["scenarios"] == second["scenarios"]


def test_replay_persists_idempotent_audit_rows_to_requested_sqlite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "replay.sqlite3"
    first = run_replay_suite(database)
    second = run_replay_suite(database)

    assert first["storage_mode"] == second["storage_mode"] == "sqlite_file"
    assert first["result_digest"] == second["result_digest"]
    with sqlite3.connect(database) as connection:
        suite = connection.execute(
            """
            SELECT passed, external_broker_mutation_calls, result_digest
            FROM replay_suites
            """
        ).fetchone()
        scenarios = connection.execute(
            """
            SELECT scenario_name, passed, external_broker_mutation_calls
            FROM replay_scenarios
            ORDER BY ordinal
            """
        ).fetchall()

    assert suite == (1, 0, first["result_digest"])
    assert len(scenarios) == 5
    assert all(passed == 1 and external_calls == 0 for _, passed, external_calls in scenarios)
