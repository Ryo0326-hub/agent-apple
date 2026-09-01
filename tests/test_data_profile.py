from __future__ import annotations

from pathlib import Path

import yaml

from thetatrap.data_profile import ALPACA_BASIC_INDICATIVE
from thetatrap.report import build_operational_report
from thetatrap.storage import Store


ROOT = Path(__file__).resolve().parents[1]


def test_profile_status_is_public_safe_and_explicit() -> None:
    status = ALPACA_BASIC_INDICATIVE.status()

    assert status["profile_id"] == "alpaca_basic_iex_indicative_v1"
    assert status["provider"] == "alpaca"
    assert status["plan"] == "basic"
    assert status["stock_feed"] == "iex"
    assert status["option_feed"] == "indicative"
    assert status["consolidated_stock_quotes"] is False
    assert status["consolidated_option_quotes"] is False
    assert len(status["limitations"]) == 3


def test_worker_compose_defaults_to_the_approved_profile() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["worker"]["environment"]

    assert environment["ALPACA_STOCK_FEED"] == "${ALPACA_STOCK_FEED:-iex}"
    assert environment["ALPACA_OPTION_FEED"] == (
        "${ALPACA_OPTION_FEED:-indicative}"
    )


def test_profile_status_survives_heartbeat_into_report_metadata(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "profile.sqlite3")
    store.initialize()
    status = ALPACA_BASIC_INDICATIVE.status()
    store.record_heartbeat(
        status="healthy",
        environment="competition",
        account_suffix="…123456",
        mcp_schema_hash="schema-hash",
        market_is_open=True,
        detail={"market_data_profile": status},
    )

    report = build_operational_report(store.path, environment="competition")

    assert report["health"]["detail"]["market_data_profile"] == status
