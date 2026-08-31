"""Defensive normalization of official Alpaca MCP market-data responses."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from thetatrap.domain import OptionSnapshot, UnderlyingQuote
from thetatrap.errors import MCPToolError
from thetatrap.mcp.client import unwrap_data


MAX_PAGES = 25


class SystemReadConnection(Protocol):
    async def call_system_read(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MarketCollection:
    collection_id: str
    symbol: str
    collected_at: datetime
    previous_trading_day: date
    underlying: UnderlyingQuote
    front_chain: tuple[OptionSnapshot, ...]
    back_chain: tuple[OptionSnapshot, ...]
    trade_expiration: date
    term_expiration: date
    source_digest: str
    diagnostics: dict[str, Any]


async def collect_symbol_market(
    connection: SystemReadConnection,
    *,
    symbol: str,
    trade_expiration: date,
    term_expiration: date,
    now: datetime | None = None,
) -> MarketCollection:
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("market collection requires a symbol")

    contract_payloads = await _fetch_contracts(
        connection,
        normalized_symbol,
        trade_expiration,
        term_expiration,
    )
    front_snapshots = await _fetch_chain(
        connection, normalized_symbol, trade_expiration
    )
    back_snapshots = await _fetch_chain(
        connection, normalized_symbol, term_expiration
    )
    calendar_reference = (now or datetime.now(UTC)).astimezone(UTC)
    previous_day = await previous_trading_day(
        connection, calendar_reference.date()
    )
    # Fetch the short-lived underlying quote last.  Strategy policy allows only
    # ten seconds of age, while the option snapshots have a sixty-second bound.
    stock_wrapper = await connection.call_system_read(
        "get_stock_latest_quote", {"symbols": normalized_symbol, "feed": "iex"}
    )
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)

    underlying_payload = _symbol_mapping(
        unwrap_data(stock_wrapper), "quotes", normalized_symbol
    )
    underlying = UnderlyingQuote.from_mapping(underlying_payload)
    contracts = {
        str(payload.get("symbol") or "").strip(): payload
        for payload in contract_payloads
        if isinstance(payload, dict) and payload.get("symbol")
    }
    front, missing_front = _join_snapshots(contracts, front_snapshots)
    back, missing_back = _join_snapshots(contracts, back_snapshots)
    if not front or not back:
        raise MCPToolError(
            f"MCP option data did not produce joined front/back chains for {normalized_symbol}"
        )

    digest_payload = {
        "symbol": normalized_symbol,
        "collected_at": observed_at.isoformat(),
        "stock_quote": underlying_payload,
        "contracts": contract_payloads,
        "front": front_snapshots,
        "back": back_snapshots,
        "previous_trading_day": previous_day.isoformat(),
    }
    source_digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return MarketCollection(
        collection_id=str(uuid.uuid4()),
        symbol=normalized_symbol,
        collected_at=observed_at,
        previous_trading_day=previous_day,
        underlying=underlying,
        front_chain=tuple(front),
        back_chain=tuple(back),
        trade_expiration=trade_expiration,
        term_expiration=term_expiration,
        source_digest=source_digest,
        diagnostics={
            "contract_count": len(contracts),
            "front_snapshot_count": len(front_snapshots),
            "back_snapshot_count": len(back_snapshots),
            "front_joined_count": len(front),
            "back_joined_count": len(back),
            "missing_front_metadata": missing_front,
            "missing_back_metadata": missing_back,
            "stock_feed": "iex",
            "option_feed": "indicative",
        },
    )


async def previous_trading_day(
    connection: SystemReadConnection, today: date
) -> date:
    start = today - timedelta(days=10)
    wrapper = await connection.call_system_read(
        "get_calendar", {"start": start.isoformat(), "end": today.isoformat()}
    )
    data = unwrap_data(wrapper)
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise MCPToolError("get_calendar did not return data.result as an array")
    days: list[date] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("date"):
            continue
        try:
            parsed = date.fromisoformat(str(row["date"])[:10])
        except ValueError:
            continue
        if parsed < today:
            days.append(parsed)
    if not days:
        raise MCPToolError("get_calendar did not include a previous trading day")
    return max(days)


async def _fetch_contracts(
    connection: SystemReadConnection,
    symbol: str,
    first_expiration: date,
    last_expiration: date,
) -> list[dict[str, Any]]:
    arguments: dict[str, Any] = {
        "underlying_symbols": symbol,
        "status": "active",
        "expiration_date_gte": first_expiration.isoformat(),
        "expiration_date_lte": last_expiration.isoformat(),
        "limit": 10000,
        "show_deliverables": True,
    }
    rows: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        wrapper = await connection.call_system_read("get_option_contracts", arguments)
        data = unwrap_data(wrapper)
        if not isinstance(data, dict):
            raise MCPToolError("get_option_contracts data must be an object")
        page = data.get("option_contracts")
        if not isinstance(page, list):
            raise MCPToolError("get_option_contracts omitted option_contracts array")
        rows.extend(item for item in page if isinstance(item, dict))
        token = data.get("next_page_token")
        if not token:
            return rows
        arguments = {**arguments, "page_token": str(token)}
    raise MCPToolError("get_option_contracts exceeded pagination safety limit")


async def _fetch_chain(
    connection: SystemReadConnection, symbol: str, expiration: date
) -> dict[str, dict[str, Any]]:
    arguments: dict[str, Any] = {
        "underlying_symbol": symbol,
        "expiration_date": expiration.isoformat(),
        "feed": "indicative",
        "limit": 1000,
    }
    snapshots: dict[str, dict[str, Any]] = {}
    for _ in range(MAX_PAGES):
        wrapper = await connection.call_system_read("get_option_chain", arguments)
        data = unwrap_data(wrapper)
        if not isinstance(data, dict):
            raise MCPToolError("get_option_chain data must be an object")
        page = data.get("snapshots")
        if not isinstance(page, dict):
            raise MCPToolError("get_option_chain omitted snapshots object")
        for contract_symbol, item in page.items():
            if isinstance(item, dict):
                snapshots[str(contract_symbol)] = item
        token = data.get("next_page_token")
        if not token:
            return snapshots
        arguments = {**arguments, "page_token": str(token)}
    raise MCPToolError("get_option_chain exceeded pagination safety limit")


def _join_snapshots(
    contracts: dict[str, dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
) -> tuple[list[OptionSnapshot], int]:
    joined: list[OptionSnapshot] = []
    missing_contracts = 0
    for symbol in sorted(snapshots):
        contract = contracts.get(symbol)
        if contract is None:
            missing_contracts += 1
            continue
        snapshot = snapshots[symbol]
        latest_quote = snapshot.get("latestQuote") or snapshot.get("latest_quote")
        if not isinstance(latest_quote, dict):
            continue
        quote = {
            **latest_quote,
            "impliedVolatility": snapshot.get(
                "impliedVolatility", snapshot.get("implied_volatility")
            ),
            "greeks": snapshot.get("greeks"),
        }
        try:
            joined.append(OptionSnapshot.from_mappings(contract, quote))
        except ValueError:
            continue
    return joined, missing_contracts


def _symbol_mapping(data: Any, key: str, symbol: str) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get(key), dict):
        raise MCPToolError(f"MCP market response omitted {key} object")
    payload = data[key].get(symbol)
    if not isinstance(payload, dict):
        raise MCPToolError(f"MCP market response omitted {symbol}")
    return payload
