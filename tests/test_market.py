from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from thetatrap.market import collect_symbol_market


def wrapped(data: Any) -> dict[str, Any]:
    return {"_alpaca_mcp_security": {}, "data": data}


def contract(symbol: str, expiration: str, right: str, strike: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "underlying_symbol": "TEST",
        "expiration_date": expiration,
        "type": right,
        "strike_price": strike,
        "tradable": True,
        "status": "active",
        "multiplier": "100",
        "size": "100",
        "open_interest": "100",
        "open_interest_date": "2026-08-28",
        "ppind": True,
        "deliverables": [],
    }


def snapshot(timestamp: str) -> dict[str, Any]:
    return {
        "latestQuote": {"bp": 2.4, "ap": 2.6, "t": timestamp},
        "impliedVolatility": 0.6,
        "greeks": {"delta": 0.5},
    }


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.contract_page = 0

    async def call_system_read(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = arguments or {}
        self.calls.append((tool_name, args))
        if tool_name == "get_stock_latest_quote":
            return wrapped(
                {
                    "quotes": {
                        "TEST": {
                            "bp": 99.99,
                            "ap": 100.01,
                            "t": "2026-08-30T15:00:00Z",
                        }
                    }
                }
            )
        if tool_name == "get_option_contracts":
            if "page_token" not in args:
                return wrapped(
                    {
                        "option_contracts": [
                            contract("TEST260904P00100000", "2026-09-04", "put", "100"),
                            contract("TEST260904C00100000", "2026-09-04", "call", "100"),
                        ],
                        "next_page_token": "next",
                    }
                )
            return wrapped(
                {
                    "option_contracts": [
                        contract("TEST260911P00100000", "2026-09-11", "put", "100"),
                        contract("TEST260911C00100000", "2026-09-11", "call", "100"),
                    ],
                    "next_page_token": None,
                }
            )
        if tool_name == "get_option_chain":
            expiration = args["expiration_date"]
            compact = expiration.replace("-", "")[2:]
            return wrapped(
                {
                    "snapshots": {
                        f"TEST{compact}P00100000": snapshot("2026-08-30T15:00:00Z"),
                        f"TEST{compact}C00100000": snapshot("2026-08-30T15:00:00Z"),
                    },
                    "next_page_token": None,
                }
            )
        if tool_name == "get_calendar":
            return wrapped(
                {
                    "result": [
                        {"date": "2026-08-28", "open": "09:30", "close": "16:00"},
                        {"date": "2026-08-31", "open": "09:30", "close": "16:00"},
                    ]
                }
            )
        raise AssertionError(tool_name)


@pytest.mark.asyncio
async def test_collection_uses_exact_basic_feeds_and_joins_metadata() -> None:
    connection = FakeConnection()
    result = await collect_symbol_market(
        connection,
        symbol="test",
        trade_expiration=date(2026, 9, 4),
        term_expiration=date(2026, 9, 11),
        now=datetime(2026, 8, 30, 15, 0, 1, tzinfo=UTC),
    )
    assert result.symbol == "TEST"
    assert result.previous_trading_day == date(2026, 8, 28)
    assert len(result.front_chain) == 2
    assert len(result.back_chain) == 2
    assert result.diagnostics["option_feed"] == "indicative"
    assert result.diagnostics["market_data_profile"]["profile_id"] == (
        "alpaca_basic_iex_indicative_v1"
    )
    assert result.diagnostics["contract_count"] == 4
    assert any(
        name == "get_stock_latest_quote" and args["feed"] == "iex"
        for name, args in connection.calls
    )
    call_names = [name for name, _ in connection.calls]
    assert call_names.index("get_stock_latest_quote") > max(
        index
        for index, name in enumerate(call_names)
        if name in {"get_option_chain", "get_calendar"}
    )
    option_calls = [args for name, args in connection.calls if name == "get_option_chain"]
    assert option_calls and all(args["feed"] == "indicative" for args in option_calls)
    contract_calls = [
        args for name, args in connection.calls if name == "get_option_contracts"
    ]
    assert contract_calls[0]["show_deliverables"] is True
    assert contract_calls[1]["page_token"] == "next"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stock_feed", "option_feed", "message"),
    [
        ("sip", "indicative", "ALPACA_STOCK_FEED"),
        ("iex", "opra", "ALPACA_OPTION_FEED"),
    ],
)
async def test_collection_rejects_unapproved_feed_before_any_mcp_read(
    stock_feed: str, option_feed: str, message: str
) -> None:
    connection = FakeConnection()

    with pytest.raises(ValueError, match=message):
        await collect_symbol_market(
            connection,
            symbol="TEST",
            trade_expiration=date(2026, 9, 4),
            term_expiration=date(2026, 9, 11),
            stock_feed=stock_feed,
            option_feed=option_feed,
        )

    assert connection.calls == []
