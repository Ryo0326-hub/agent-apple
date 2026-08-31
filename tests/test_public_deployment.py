from __future__ import annotations

import ast
import json
from pathlib import Path

import yaml

from thetatrap.dashboard import display_build_sha as operator_build_sha
from thetatrap.public_dashboard import (
    REDACTED,
    build_public_view,
    display_build_sha as public_build_sha,
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
            "latest_session": {
                "session_id": "session-private",
                "status": "healthy",
                "package_version": "2.3.0",
                "tool_count": 54,
            }
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
            }
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
            },
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
    ):
        assert private_value not in serialized
    assert REDACTED in serialized
    assert view["account"]["suffix"] == "…174000"
    assert view["health"]["stale"] is True
    assert view["orders"]["chain_count"] == 1
    assert view["orders"]["fill_count"] == 4
    assert view["portfolio"]["observed_change"] == "70.00"


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
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
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
