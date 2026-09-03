from __future__ import annotations

import ast
import json
from pathlib import Path

import yaml

from thetatrap.dashboard import display_build_sha as operator_build_sha
from thetatrap.public_dashboard import (
    REDACTED,
    _primary_execution_candidate,
    _primary_execution_review,
    _review_display_outcome,
    build_public_view,
    display_build_sha as public_build_sha,
    serialize_public_evidence,
)


ROOT = Path(__file__).resolve().parents[1]


def test_public_projection_recursively_removes_private_identifiers() -> None:
    account_uuid = "123e4567-e89b-42d3-a456-426614174000"
    report = {
        "mode": {
            "environment": "competition",
            "paper": True,
            "data_feed": "BASIC INDICATIVE",
            "trading_state": "DISARMED",
            "banner": "PAPER · TRADING DISARMED",
        },
        "identity": {"account_suffix": "…174000", "account_id": account_uuid},
        "health": {"status": "healthy", "observed_at": "now"},
        "mcp": {
            "operational_status": "ready",
            "latest_session": {
                "session_id": "session-private",
                "status": "closed",
                "package_version": "2.3.0",
                "tool_count": 54,
            },
            "timeline": [
                {
                    "called_at": "2026-09-02T18:50:00Z",
                    "principal": "agent",
                    "tool_name": "get_clock",
                    "status": "ok",
                    "duration_ms": 11,
                }
            ],
        },
        "kill_switch": {"known": True, "enabled": False},
        "one_shot_entry": {
            "state": "ARMED",
            "strategy_date": "2026-09-02",
            "authorization_id": "authorization-private",
        },
        "strategy": {
            "current_run": {
                "run_id": "run-private",
                "state": "POLICY_CHECK",
                "strategy_date": "2026-09-02",
            },
            "run_history": [{"run_id": "run-private"}],
            "transition_history": [
                {
                    "run_id": "run-private",
                    "strategy_date": "2026-09-02",
                    "from_state": "SCREENING",
                    "to_state": "AI_REVIEW",
                    "reason_code": "CANDIDATE_ELIGIBLE",
                    "transitioned_at": "2026-09-02T18:50:00Z",
                }
            ],
        },
        "candidate": {
            "selected": {
                "candidate_id": "candidate-private",
                "symbol": "SNOW",
                "candidate_rank": 1,
                "eligible": True,
                "payload": {
                    "maximum_loss": "450.00",
                    "account_id": account_uuid,
                    "order_id": "order-private",
                },
            },
            "latest_gate_outcomes": [
                {"gate_name": "MAX_LOSS", "passed": True, "candidate_id": "private"}
            ],
            "history": [
                {
                    "candidate_id": "candidate-private",
                    "run_id": "run-private",
                    "strategy_date": "2026-09-02",
                    "scanned_at": "2026-09-02T18:50:00Z",
                    "symbol": "SNOW",
                    "candidate_rank": 1,
                    "eligible": True,
                    "payload": {
                        "iv_ratio": "1.20",
                        "maximum_loss": "450.00",
                    },
                    "gates": [
                        {
                            "candidate_id": "candidate-private",
                            "gate_name": "ALL_DETERMINISTIC_GATES",
                            "passed": True,
                        }
                    ],
                },
                {
                    "candidate_id": "candidate-rejected-private",
                    "run_id": "run-private",
                    "strategy_date": "2026-09-02",
                    "scanned_at": "2026-09-02T18:49:00Z",
                    "symbol": "AVGO",
                    "eligible": False,
                    "payload": {
                        "failures": [
                            {
                                "code": "IV_RATIO_LOW",
                                "detail": "below frozen minimum",
                            }
                        ]
                    },
                    "failed_gate_names": ["IV_RATIO_LOW"],
                },
            ],
            "scan_matrix": [
                {
                    "symbol": "SNOW",
                    "event_date": "2026-09-02",
                    "configured_status": "VERIFIED",
                    "latest_result": "ELIGIBLE",
                    "evaluation_count": 1,
                    "eligible_count": 1,
                    "latest_scanned_at": "2026-09-02T18:50:00Z",
                    "latest_payload": {
                        "iv_ratio": "1.20",
                        "maximum_loss": "450.00",
                    },
                }
            ],
        },
        "agent": {
            "latest_run": {
                "agent_run_id": "agent-private",
                "model": "Qwen/Qwen3-Coder-Next",
                "status": "COMPLETED",
                "result": {"decision": "ALLOW"},
            },
            "tool_trace": [
                {
                    "sequence": 0,
                    "is_official_mcp": True,
                    "tool_name": "get_orders",
                    "status": "ok",
                    "arguments": {
                        "order_id": "order-private",
                        "account_id": account_uuid,
                    },
                }
            ],
            "reviews": [
                {
                    "agent_run_id": "agent-private",
                    "run_id": "run-private",
                    "candidate_id": "candidate-private",
                    "strategy_date": "2026-09-02",
                    "symbol": "SNOW",
                    "model": "Qwen/Qwen3-Coder-Next",
                    "status": "COMPLETED",
                    "result": {"outcome": "ALLOW"},
                    "tool_trace": [
                        {
                            "sequence": 0,
                            "is_official_mcp": True,
                            "tool_name": "get_clock",
                            "status": "ok",
                        }
                    ],
                }
            ],
            "advisories": [
                {
                    "advisory_run_id": "advisory-private",
                    "run_id": "run-private",
                    "candidate_id": "candidate-rejected-private",
                    "strategy_date": "2026-09-02",
                    "symbol": "AVGO",
                    "mode": "READ_ONLY_REJECTED_CANDIDATE_ADVISORY",
                    "model": "Qwen/Qwen3-Coder-Next",
                    "status": "COMPLETED",
                    "result": {
                        "assessment": "DETERMINISTIC_REJECTION_CONFIRMED",
                        "summary": "The IV-ratio gate remains binding.",
                        "evidence": ["Observed ratio is below the frozen minimum."],
                        "non_authorizing": True,
                    },
                    "tool_trace": [
                        {
                            "advisory_run_id": "advisory-private",
                            "sequence": 0,
                            "turn": 1,
                            "tool_name": "get_clock",
                            "arguments_hash": "a" * 64,
                            "result_hash": "b" * 64,
                            "status": "ok",
                            "duration_ms": 4,
                        }
                    ],
                }
            ],
        },
        "orders": {
            "chain_count": 1,
            "fill_count": 4,
            "option_cash_flow_ex_fees": "70.00",
            "chains": [
                {
                    "chain_id": "chain-private",
                    "purpose": "entry",
                    "state": "FILLED",
                    "attempts": [{"attempt_id": "attempt-private"}],
                    "fills": [{"fill_id": "fill-private"}] * 4,
                }
            ],
        },
        "portfolio": {
            "latest_position_observation": {"is_flat": True, "run_id": "private"},
            "equity": {
                "first": "100000.00",
                "latest": "100070.00",
                "observed_change": "70.00",
                "history": [
                    {
                        "observed_at": "2026-09-02T18:50:00Z",
                        "equity": "100070.00",
                        "buying_power": "99500.00",
                    }
                ],
            },
        },
        "safety": {
            "kill_switch": {"known": True, "enabled": False, "recent_events": []},
            "entry_permissions": [
                {
                    "authorization_id": "authorization-private",
                    "strategy_date": "2026-09-02",
                    "state": "ARMED",
                }
            ],
            "maximum_defined_loss": "500.00",
            "maximum_contracts": 1,
            "equity_kill_threshold": "99000.00",
            "read_only_viewer": True,
        },
        "data_profile": {
            "profile_id": "alpaca_basic_iex_indicative_v1",
            "provider": "alpaca",
            "plan": "basic",
            "stock_feed": "iex",
            "option_feed": "indicative",
            "consolidated_stock_quotes": False,
            "consolidated_option_quotes": False,
            "limitations": ["Not consolidated OPRA."],
        },
        "limitations": ["Paper fills are simulated."],
        "report_digest": "a" * 64,
    }

    view = build_public_view(report)
    serialized = json.dumps(view)

    for private_value in (
        account_uuid,
        "authorization-private",
        "run-private",
        "candidate-private",
        "order-private",
        "chain-private",
        "attempt-private",
        "fill-private",
        "session-private",
        "advisory-private",
        "candidate-rejected-private",
    ):
        assert private_value not in serialized
    assert REDACTED in serialized
    assert view["account"]["suffix"] == "…174000"
    assert view["health"]["stale"] is True
    assert view["mcp"]["status"] == "ready"
    assert view["orders"]["chain_count"] == 1
    assert view["orders"]["fill_count"] == 4
    assert view["orders"]["fill_evidence"] == "NORMALIZED_LEG_FILLS"
    assert view["orders"]["cash_flow_verified"] is True
    assert view["portfolio"]["observed_change"] == "70.00"
    assert [item["symbol"] for item in view["candidate"]["history"]] == [
        "SNOW",
        "AVGO",
    ]
    assert view["candidate"]["history"][1]["failed_gates"] == ["IV_RATIO_LOW"]
    snow_matrix = next(
        item for item in view["candidate"]["scan_matrix"] if item["symbol"] == "SNOW"
    )
    assert snow_matrix["latest_result"] == "ELIGIBLE"
    assert view["agent"]["reviews"][0]["decision"] == "ALLOW"
    assert view["agent"]["advisories"][0]["kind"] == "READ_ONLY_ADVISORY"
    assert view["agent"]["advisories"][0]["decision"] == (
        "DETERMINISTIC_REJECTION_CONFIRMED"
    )
    assert view["agent"]["advisories"][0]["non_authorizing"] is True
    assert view["agent"]["advisories"][0]["evidence"]
    assert view["agent"]["advisories"][0]["tool_trace"][0]["kind"] == ("read-only hash")
    assert view["mcp"]["timeline"][0]["tool"] == "get_clock"
    assert view["portfolio"]["equity_history"][0]["equity"] == 100070.0
    assert view["safety"]["read_only_viewer"] is True
    assert view["data_profile"]["option_feed"] == "indicative"
    day = next(
        item for item in view["competition_days"] if item["date"] == "2026-09-02"
    )
    assert day["scans"] == 2
    assert day["eligible"] == 1
    assert day["qwen_decisions"] == 1
    assert any(item["category"] == "SCREEN" for item in view["activity_timeline"])
    assert any(item["category"] == "QWEN" for item in view["activity_timeline"])


def test_public_projection_labels_sep3_profile_without_exposing_run_context() -> None:
    report = {
        "strategy": {
            "current_run": {
                "run_id": "run-private",
                "strategy_date": "2026-09-03",
                "strategy_version": "canary-v1",
                "state": "SCREENING",
                "context": {
                    "strategy_profile_id": "sep3_intraday_theta_canary_v1",
                    "account_id": "private-account",
                },
            },
            "run_history": [
                {
                    "run_id": "run-private",
                    "strategy_date": "2026-09-03",
                }
            ],
        },
        "candidate": {
            "history": [
                {
                    "run_id": "run-private",
                    "strategy_date": "2026-09-03",
                    "scanned_at": "2026-09-03T13:46:00Z",
                    "symbol": "QQQ",
                    "eligible": False,
                    "payload": {
                        "failures": [
                            {"code": "OPTION_SPREAD_WIDE", "detail": "bounded"}
                        ]
                    },
                }
            ]
        },
        "orders": {"chains": []},
    }

    view = build_public_view(report)

    assert view["strategy"]["profile"]["profile_id"] == (
        "sep3_intraday_theta_canary_v1"
    )
    assert view["strategy"]["profile"]["name"] == "Intraday Theta Canary"
    assert view["strategy"]["profile"]["maximum_defined_loss"] == "$80"
    assert view["safety"]["maximum_defined_loss"] == "80.00"
    assert any(
        item["symbol"] == "QQQ" and item["configured"] == "SEP3_CANARY"
        for item in view["candidate"]["scan_matrix"]
    )
    day = next(
        item for item in view["competition_days"] if item["date"] == "2026-09-03"
    )
    assert day["outcome"] == "NO TRADE · GATES HELD"
    assert "private-account" not in json.dumps(view)


def test_public_projection_uses_broker_status_as_conservative_mleg_fill_evidence() -> (
    None
):
    report = {
        "strategy": {
            "current_run": {
                "run_id": "run-private",
                "strategy_date": "2026-09-03",
                "state": "POSITION_OPEN",
            },
            "run_history": [{"run_id": "run-private", "strategy_date": "2026-09-03"}],
        },
        "orders": {
            "chain_count": 2,
            "fill_count": 0,
            "option_cash_flow_ex_fees": "0.00",
            "chains": [
                {
                    "chain_id": "chain-private",
                    "run_id": "run-private",
                    "purpose": "entry",
                    "state": "FILLED",
                    "created_at": "2026-09-03T13:47:00Z",
                    "payload": {
                        "limit_price": "-0.82",
                        "client_order_id": "order-private",
                        "legs": [{"symbol": "QQQ260904P00711000"}],
                    },
                    "attempts": [
                        {
                            "attempt_id": "attempt-private",
                            "created_at": "2026-09-03T13:46:29Z",
                            "request": {"limit_price": "-0.82"},
                        }
                    ],
                    "fills": [],
                    "status_history": [
                        {
                            "event_kind": "broker_observation",
                            "broker_status": "filled",
                            "observed_at": "2026-09-03T13:47:20Z",
                            "detail": {
                                "filled_avg_price": "-0.82",
                                "filled_qty": "1",
                                "filled_at": "2026-09-03T13:47:19Z",
                                "order_id": "broker-private",
                                "legs": [
                                    {
                                        "symbol": "QQQ260904P00711000",
                                        "side": "buy",
                                        "position_intent": "buy_to_open",
                                        "filled_qty": "1",
                                        "filled_avg_price": "2.15",
                                        "filled_at": "2026-09-03T13:47:19Z",
                                        "status": "filled",
                                    }
                                ],
                            },
                        },
                        {
                            "event_kind": "transition",
                            "broker_status": "filled",
                            "observed_at": "2026-09-03T13:47:20Z",
                            "detail": {"reconciled": True},
                        },
                    ],
                },
                {
                    "chain_id": "exit-chain-private",
                    "run_id": "run-private",
                    "purpose": "exit",
                    "state": "FILLED",
                    "created_at": "2026-09-03T19:15:00Z",
                    "payload": {"limit_price": "0.90"},
                    "attempts": [
                        {
                            "attempt_id": "exit-attempt-private",
                            "created_at": "2026-09-03T19:15:08Z",
                            "request": {"limit_price": "0.90"},
                        },
                        {
                            "attempt_id": "exit-attempt-2-private",
                            "created_at": "2026-09-03T19:16:10Z",
                            "request": {"limit_price": "0.95"},
                        },
                    ],
                    "fills": [],
                    "status_history": [
                        {
                            "event_kind": "broker_observation",
                            "broker_status": "filled",
                            "observed_at": "2026-09-03T19:16:01Z",
                            "detail": {
                                "filled_avg_price": "0.95",
                                "filled_qty": "1",
                                "filled_at": "2026-09-03T19:16:00Z",
                                "legs": [
                                    {
                                        "symbol": "QQQ260904P00711000",
                                        "side": "sell",
                                        "position_intent": "sell_to_close",
                                        "filled_qty": "1",
                                        "filled_avg_price": "0.62",
                                        "filled_at": "2026-09-03T19:16:00Z",
                                        "status": "filled",
                                    }
                                ],
                            },
                        }
                    ],
                },
            ],
        },
        "portfolio": {
            "latest_position_observation": {
                "is_flat": True,
                "payload": [],
            }
        },
    }

    view = build_public_view(report)
    chain = view["orders"]["chains"][0]
    exit_chain = view["orders"]["chains"][1]

    assert view["orders"]["broker_filled_order_count"] == 2
    assert view["orders"]["fill_count"] == 0
    assert view["orders"]["fill_evidence"] == "BROKER_ORDER_STATUS"
    assert view["orders"]["cash_flow_verified"] is False
    assert view["orders"]["broker_leg_fill_count"] == 2
    assert view["orders"]["broker_mleg_cash_flow_ex_fees"] == "-13.00"
    assert view["orders"]["broker_round_trip_confirmed"] is True
    assert chain["initial_limit_price"] == "-0.82"
    assert chain["final_limit_price"] == "-0.82"
    assert chain["symbol"] == "QQQ"
    assert chain["submitted_at"] == "2026-09-03T13:46:29Z"
    assert chain["broker_fill_price"] == "-0.82"
    assert chain["broker_filled_at"] == "2026-09-03T13:47:19Z"
    assert len(chain["broker_leg_fills"]) == 1
    assert exit_chain["initial_limit_price"] == "0.90"
    assert exit_chain["final_limit_price"] == "0.95"
    assert exit_chain["replacement_count"] == 1
    assert exit_chain["broker_fill_price"] == "0.95"
    assert exit_chain["broker_filled_at"] == "2026-09-03T19:16:00Z"
    assert view["portfolio"]["position"] == "FLAT"
    assert view["portfolio"]["position_count"] == 0
    day = next(
        item for item in view["competition_days"] if item["date"] == "2026-09-03"
    )
    assert day["outcome"] == "BROKER-CONFIRMED ROUND TRIP"
    assert day["broker_filled_orders"] == 2
    assert day["leg_fills"] == 0
    serialized = serialize_public_evidence(view)
    assert "chain-private" not in serialized
    assert "order-private" not in serialized
    assert "attempt-private" not in serialized
    assert "broker-private" not in serialized


def test_public_projection_does_not_infer_broker_fill_from_local_state() -> None:
    report = {
        "strategy": {
            "current_run": {
                "run_id": "run-private",
                "strategy_date": "2026-09-03",
                "state": "POSITION_OPEN",
            },
            "run_history": [{"run_id": "run-private", "strategy_date": "2026-09-03"}],
        },
        "orders": {
            "chains": [
                {
                    "run_id": "run-private",
                    "purpose": "entry",
                    "state": "FILLED",
                    "fills": [],
                    "status_history": [
                        {
                            "event_kind": "transition",
                            "to_state": "FILLED",
                            "broker_status": None,
                            "observed_at": "2026-09-03T13:47:20Z",
                        }
                    ],
                }
            ]
        },
    }

    view = build_public_view(report)

    assert view["orders"]["broker_filled_order_count"] == 0
    assert view["orders"]["fill_evidence"] == "NONE"
    assert view["orders"]["chains"][0]["broker_fill_confirmed"] is False


def test_sep3_successful_qqq_review_is_primary_and_policy_error_is_explained() -> None:
    report = {
        "strategy": {
            "current_run": {
                "run_id": "run-private",
                "strategy_date": "2026-09-03",
                "state": "POSITION_OPEN",
            },
            "run_history": [{"run_id": "run-private", "strategy_date": "2026-09-03"}],
        },
        "candidate": {
            "history": [
                {
                    "run_id": "run-private",
                    "strategy_date": "2026-09-03",
                    "symbol": "QQQ",
                    "candidate_rank": 2,
                    "eligible": True,
                    "scanned_at": "2026-09-03T13:47:00Z",
                    "payload": {"proposed_credit": "0.82"},
                }
            ]
        },
        "agent": {
            "reviews": [
                {
                    "strategy_date": "2026-09-03",
                    "symbol": "SPY",
                    "candidate_rank": 1,
                    "status": "FAILED",
                    "error_type": "PolicyError",
                    "ended_at": "2026-09-03T13:46:00Z",
                },
                {
                    "strategy_date": "2026-09-03",
                    "symbol": "QQQ",
                    "candidate_rank": 2,
                    "status": "COMPLETED",
                    "result": {"decision": "ALLOW"},
                    "ended_at": "2026-09-03T13:47:00Z",
                    "tool_trace": [{"tool_name": "get_candidate", "status": "ok"}],
                },
            ]
        },
        "orders": {"chains": []},
    }

    view = build_public_view(report)
    primary = _primary_execution_review(view)
    selected = _primary_execution_candidate(view)

    assert primary["symbol"] == "QQQ"
    assert primary["decision"] == "ALLOW"
    assert view["agent"]["status"] == "COMPLETED"
    assert view["agent"]["decision"] == "ALLOW"
    assert view["agent"]["error_type"] is None
    assert selected["symbol"] == "QQQ"
    spy = next(item for item in view["agent"]["reviews"] if item["symbol"] == "SPY")
    assert _review_display_outcome(spy) == "SAFETY GATEWAY BLOCKED"


def test_public_source_has_no_storage_or_operator_control_surface() -> None:
    source_path = ROOT / "src" / "thetatrap" / "public_dashboard.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "thetatrap.storage" not in imported_modules
    assert "thetatrap.dashboard" not in imported_modules

    forbidden_streamlit_calls = {
        "button",
        "checkbox",
        "form",
        "form_submit_button",
        "number_input",
        "radio",
        "selectbox",
        "slider",
        "text_area",
        "text_input",
        "toggle",
    }
    called_streamlit_members = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
    }
    assert called_streamlit_members.isdisjoint(forbidden_streamlit_calls)
    assert "update_kill_switch" not in source
    assert "Store(" not in source
    assert '@st.fragment(run_every="30s")' in source
    assert "Worker evidence is stale or missing" in source
    assert "All-symbol scan matrix" in source
    assert "Rejected-candidate advisories are read-only" in source
    assert "BASIC INDICATIVE OPTIONS DATA" in source
    assert "no mutation callbacks" in source
    assert "prefers-color-scheme: dark" in source
    assert "--tt-card: #17241e" in source
    assert '[data-testid="stMetricValue"] p' in source
    assert "Intraday Theta Canary" in source
    assert "not an automatic fallback" in source
    assert "SAFETY GATEWAY BLOCKED" in source
    assert "Download sanitized public evidence (JSON)" in source
    headings = [
        "00 · RESULT SNAPSHOT",
        "01 · PROBLEM & STRATEGY",
        "02 · AI + ALPACA MCP PROOF",
        "03 · VERIFIED RESULTS",
        "04 · SAFETY & ROBUSTNESS",
        "05 · LIMITATIONS & EVIDENCE",
    ]
    offsets = [source.index(heading) for heading in headings]
    assert offsets == sorted(offsets)


def test_build_revisions_are_strictly_bounded() -> None:
    for display in (operator_build_sha, public_build_sha):
        assert display("A" * 40) == "aaaaaaaaaaaa"
        assert display("main") == "unversioned"
        assert display("<script>alert(1)</script>") == "unversioned"


def test_production_overlay_isolates_public_viewer_from_controls_and_secrets() -> None:
    base = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    production = yaml.safe_load(
        (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    )
    services = production["services"]
    public = services["public-ui"]
    proxy = services["caddy"]

    assert base["services"]["ui"]["ports"] == ["127.0.0.1:8501:8501"]
    assert "src/thetatrap/dashboard.py" in base["services"]["ui"]["command"]
    assert "src/thetatrap/public_dashboard.py" in public["command"]

    public_environment = public["environment"]
    assert not any(
        key.startswith("ALPACA_") or key.startswith("FEATHERLESS_")
        for key in public_environment
    )
    assert public["volumes"] == ["thetatrap-data:/data:ro"]
    assert public["read_only"] is True
    assert public["cap_drop"] == ["ALL"]
    assert "ports" not in public
    assert "depends_on" not in public
    assert public["networks"] == ["edge"]

    assert proxy["networks"] == ["edge"]
    assert set(proxy["ports"]) == {"80:80", "443:443", "443:443/udp"}
    assert proxy["security_opt"] == ["no-new-privileges:true"]
    assert proxy["healthcheck"]["test"][1] == "wget"

    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "reverse_proxy public-ui:8501" in caddyfile
    assert "reverse_proxy ui:" not in caddyfile
    assert "noindex, nofollow" in caddyfile
