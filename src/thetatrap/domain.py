"""Pure domain models for deterministic option-strategy evaluation.

The MCP adapter is responsible for extracting broker payloads.  These models
provide a small normalization boundary so the strategy never performs math on
binary floating-point values or unvalidated timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping


ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"

    @classmethod
    def parse(cls, value: object) -> "OptionRight":
        normalized = str(value).strip().lower()
        aliases = {"c": cls.CALL, "call": cls.CALL, "p": cls.PUT, "put": cls.PUT}
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"unsupported option type: {value!r}") from exc


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class LegRole(StrEnum):
    LONG_PUT = "long_put"
    SHORT_PUT = "short_put"
    SHORT_CALL = "short_call"
    LONG_CALL = "long_call"


@dataclass(frozen=True, slots=True)
class UnderlyingQuote:
    bid: Decimal
    ask: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "underlying quote timestamp")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "UnderlyingQuote":
        return cls(
            bid=as_decimal(_first(payload, "bid_price", "bp", "bid"), "underlying bid"),
            ask=as_decimal(_first(payload, "ask_price", "ap", "ask"), "underlying ask"),
            timestamp=as_datetime(
                _first(payload, "timestamp", "t"), "underlying quote timestamp"
            ),
        )

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class OptionContract:
    symbol: str
    underlying_symbol: str
    expiration: date
    right: OptionRight
    strike: Decimal
    tradable: bool
    status: str
    multiplier: Decimal
    size: Decimal
    open_interest: int | None
    open_interest_date: date | None
    ppind: bool | None = None
    has_custom_deliverables: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "OptionContract":
        open_interest = payload.get("open_interest")
        open_interest_date = payload.get("open_interest_date")
        underlying_symbol = str(
            _first(payload, "underlying_symbol", "root_symbol")
        ).strip()
        return cls(
            symbol=str(_first(payload, "symbol")).strip(),
            underlying_symbol=underlying_symbol,
            expiration=as_date(
                _first(payload, "expiration_date", "expiration"), "expiration"
            ),
            right=OptionRight.parse(_first(payload, "type", "right")),
            strike=as_decimal(
                _first(payload, "strike_price", "strike"), "strike price"
            ),
            tradable=as_bool(_first(payload, "tradable"), "tradable"),
            status=str(_first(payload, "status")).strip().lower(),
            multiplier=as_decimal(payload.get("multiplier", "100"), "multiplier"),
            size=as_decimal(payload.get("size", "100"), "size"),
            open_interest=(
                as_nonnegative_int(open_interest, "open interest")
                if open_interest not in (None, "")
                else None
            ),
            open_interest_date=(
                as_date(open_interest_date, "open interest date")
                if open_interest_date not in (None, "")
                else None
            ),
            ppind=as_optional_bool(payload.get("ppind"), "ppind"),
            has_custom_deliverables=not _deliverables_are_standard(
                payload.get("deliverables"), underlying_symbol
            ),
        )

    @property
    def is_standard(self) -> bool:
        return (
            self.multiplier == ONE_HUNDRED
            and self.size == ONE_HUNDRED
            and not self.has_custom_deliverables
        )


@dataclass(frozen=True, slots=True)
class OptionQuote:
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "option quote timestamp")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "OptionQuote":
        greeks = payload.get("greeks")
        greeks_mapping = greeks if isinstance(greeks, Mapping) else {}
        iv_value = _first_optional(
            payload, "implied_volatility", "impliedVolatility", "iv"
        )
        delta_value = _first_optional(payload, "delta")
        if delta_value is None:
            delta_value = _first_optional(greeks_mapping, "delta")
        return cls(
            bid=as_decimal(_first(payload, "bid_price", "bp", "bid"), "option bid"),
            ask=as_decimal(_first(payload, "ask_price", "ap", "ask"), "option ask"),
            timestamp=as_datetime(
                _first(payload, "timestamp", "t"), "option quote timestamp"
            ),
            implied_volatility=(
                as_decimal(iv_value, "implied volatility")
                if iv_value not in (None, "")
                else None
            ),
            delta=(
                as_decimal(delta_value, "delta")
                if delta_value not in (None, "")
                else None
            ),
        )

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def relative_spread(self) -> Decimal | None:
        midpoint = self.midpoint
        return self.spread / midpoint if midpoint > ZERO else None


@dataclass(frozen=True, slots=True)
class OptionSnapshot:
    contract: OptionContract
    quote: OptionQuote

    @classmethod
    def from_mappings(
        cls, contract: Mapping[str, Any], quote: Mapping[str, Any]
    ) -> "OptionSnapshot":
        return cls(
            contract=OptionContract.from_mapping(contract),
            quote=OptionQuote.from_mapping(quote),
        )


@dataclass(frozen=True, slots=True)
class StrategyLeg:
    role: LegRole
    side: OrderSide
    snapshot: OptionSnapshot

    @property
    def position_sign(self) -> Decimal:
        return Decimal("1") if self.side is OrderSide.BUY else Decimal("-1")


@dataclass(frozen=True, slots=True)
class CondorCandidate:
    symbol: str
    observed_at: datetime
    spot: Decimal
    front_atm_strike: Decimal
    back_atm_strike: Decimal
    expected_move: Decimal
    expected_move_fraction: Decimal
    front_atm_iv: Decimal
    back_atm_iv: Decimal
    iv_ratio: Decimal
    long_put: StrategyLeg
    short_put: StrategyLeg
    short_call: StrategyLeg
    long_call: StrategyLeg
    wing_width: Decimal
    natural_credit: Decimal
    midpoint_credit: Decimal
    proposed_credit: Decimal
    tick_size: Decimal
    maximum_profit: Decimal
    maximum_loss: Decimal
    risk_budget: Decimal
    quantity: int
    aggregate_relative_spread: Decimal
    net_delta: Decimal | None

    @property
    def legs(self) -> tuple[StrategyLeg, StrategyLeg, StrategyLeg, StrategyLeg]:
        return (self.long_put, self.short_put, self.short_call, self.long_call)

    @property
    def credit_to_width(self) -> Decimal:
        return self.natural_credit / self.wing_width


class GateCode(StrEnum):
    UNDERLYING_QUOTE_INVALID = "UNDERLYING_QUOTE_INVALID"
    UNDERLYING_QUOTE_STALE = "UNDERLYING_QUOTE_STALE"
    UNDERLYING_SPREAD_WIDE = "UNDERLYING_SPREAD_WIDE"
    FRONT_ATM_PAIR_MISSING = "FRONT_ATM_PAIR_MISSING"
    BACK_ATM_PAIR_MISSING = "BACK_ATM_PAIR_MISSING"
    ATM_QUOTE_INVALID = "ATM_QUOTE_INVALID"
    ATM_QUOTE_STALE = "ATM_QUOTE_STALE"
    ATM_SPREAD_WIDE = "ATM_SPREAD_WIDE"
    ATM_IV_MISSING = "ATM_IV_MISSING"
    IV_RATIO_LOW = "IV_RATIO_LOW"
    EXPECTED_MOVE_OUT_OF_RANGE = "EXPECTED_MOVE_OUT_OF_RANGE"
    SHORT_STRIKE_MISSING = "SHORT_STRIKE_MISSING"
    SYMMETRIC_WINGS_MISSING = "SYMMETRIC_WINGS_MISSING"
    CONTRACT_INELIGIBLE = "CONTRACT_INELIGIBLE"
    OPEN_INTEREST_MISSING_OR_STALE = "OPEN_INTEREST_MISSING_OR_STALE"
    OPEN_INTEREST_LOW = "OPEN_INTEREST_LOW"
    OPTION_QUOTE_INVALID = "OPTION_QUOTE_INVALID"
    OPTION_QUOTE_STALE = "OPTION_QUOTE_STALE"
    OPTION_SPREAD_WIDE = "OPTION_SPREAD_WIDE"
    CREDIT_TOO_LOW = "CREDIT_TOO_LOW"
    CREDIT_NOT_DEFINED_RISK = "CREDIT_NOT_DEFINED_RISK"
    MIDPOINT_NATURAL_GAP_WIDE = "MIDPOINT_NATURAL_GAP_WIDE"
    NET_DELTA_HIGH = "NET_DELTA_HIGH"
    MAXIMUM_LOSS_HIGH = "MAXIMUM_LOSS_HIGH"
    BUYING_POWER_LOW = "BUYING_POWER_LOW"
    QUANTITY_ZERO = "QUANTITY_ZERO"
    NO_VALID_CONDOR = "NO_VALID_CONDOR"


@dataclass(frozen=True, slots=True)
class GateFailure:
    code: GateCode
    detail: str


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    symbol: str
    candidate: CondorCandidate | None
    failures: tuple[GateFailure, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.candidate is not None

    @property
    def failure_codes(self) -> tuple[GateCode, ...]:
        return tuple(failure.code for failure in self.failures)


def as_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be a decimal value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal value") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def as_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    _require_aware(result, field_name)
    return result.astimezone(UTC)


def as_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 date") from exc
    raise ValueError(f"{field_name} must be an ISO-8601 date")


def as_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError(f"{field_name} must be a boolean")


def as_optional_bool(value: object, field_name: str) -> bool | None:
    return None if value in (None, "") else as_bool(value, field_name)


def as_nonnegative_int(value: object, field_name: str) -> int:
    number = as_decimal(value, field_name)
    if number < ZERO or number != number.to_integral_value():
        raise ValueError(f"{field_name} must be a non-negative integer")
    return int(number)


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    value = _first_optional(payload, *keys)
    if value is None:
        raise ValueError(f"missing required field: {'/'.join(keys)}")
    return value


def _first_optional(payload: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")


def _deliverables_are_standard(value: object, underlying_symbol: str) -> bool:
    """Treat omitted deliverables as standard; validate them when requested."""

    if value in (None, []):
        return True
    if not isinstance(value, list) or len(value) != 1:
        return False
    deliverable = value[0]
    if not isinstance(deliverable, Mapping):
        return False
    try:
        return (
            str(deliverable.get("type", "")).strip().lower() == "equity"
            and str(deliverable.get("symbol", "")).strip().upper()
            == underlying_symbol.upper()
            and as_decimal(deliverable.get("amount"), "deliverable amount")
            == ONE_HUNDRED
            and as_decimal(
                deliverable.get("allocation_percentage"),
                "deliverable allocation percentage",
            )
            == ONE_HUNDRED
            and as_optional_bool(
                deliverable.get("delayed_settlement", False),
                "deliverable delayed settlement",
            )
            is False
        )
    except ValueError:
        return False
