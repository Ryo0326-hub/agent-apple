"""Pure construction of immutable Alpaca MLEG order intents.

This module deliberately has no broker, database, clock, or model dependency.
It converts an already-approved :class:`~thetatrap.domain.CondorCandidate` into
the exact wire payload accepted by the pinned Alpaca MCP server.  Every payload
is checked by the same policy validator used immediately before execution.

Domain prices remain positive ``Decimal`` values.  Only the entry wire payload
uses Alpaca's negative-credit convention.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Literal, Mapping

from thetatrap.domain import (
    CondorCandidate,
    LegRole,
    OptionRight,
    OrderSide,
    StrategyEvaluation,
    StrategyLeg,
)
from thetatrap.errors import PolicyError
from thetatrap.policy import payload_hash, validate_mleg_arguments


_SLUG_CHARACTER = re.compile(r"[^a-z0-9]+")
_MAX_IDENTIFIER_LENGTH = 128
_HUNDRED = Decimal("100")


class OrderPurpose(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class OrderIdentifiers:
    """Stable logical IDs plus one sequence-specific broker attempt ID."""

    intent_id: str
    chain_id: str
    attempt_id: str
    client_order_id: str


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """An immutable, validated exact order request.

    ``arguments_json`` is the immutable source of truth.  ``arguments`` parses
    a new native dict/list tree on every access so callers cannot mutate the
    persisted intent by retaining and changing a reference.
    """

    purpose: OrderPurpose
    sequence: int
    identifiers: OrderIdentifiers
    arguments_json: str
    arguments_hash: str

    @property
    def intent_id(self) -> str:
        return self.identifiers.intent_id

    @property
    def chain_id(self) -> str:
        return self.identifiers.chain_id

    @property
    def attempt_id(self) -> str:
        return self.identifiers.attempt_id

    @property
    def client_order_id(self) -> str:
        return self.identifiers.client_order_id

    @property
    def arguments(self) -> dict[str, Any]:
        payload = json.loads(self.arguments_json)
        if not isinstance(payload, dict):  # pragma: no cover - constructor invariant
            raise PolicyError("stored order intent is not an object")
        return payload

    def as_record(self) -> dict[str, Any]:
        """Return a primitive SQLite/audit representation without credentials."""

        return {
            "intent_id": self.intent_id,
            "chain_id": self.chain_id,
            "attempt_id": self.attempt_id,
            "client_order_id": self.client_order_id,
            "purpose": self.purpose.value,
            "sequence": self.sequence,
            "arguments": self.arguments,
            "arguments_hash": self.arguments_hash,
        }


def build_entry_order_intent(
    candidate: CondorCandidate,
    *,
    environment: str,
    account_id: str,
    event_date: date,
    strategy_version: str,
    sequence: int = 0,
) -> OrderIntent:
    """Build one exact, four-leg credit entry request.

    ``sequence`` is zero for the initial submission.  A later deterministic
    replacement keeps the logical intent/chain IDs and changes only the
    attempt/client IDs.
    """

    expiration = _validate_candidate(candidate)
    identifiers = derive_order_identifiers(
        environment=environment,
        account_id=account_id,
        event_date=event_date,
        strategy_version=strategy_version,
        symbol=candidate.symbol,
        expiration=expiration,
        purpose=OrderPurpose.ENTRY,
        sequence=sequence,
    )
    arguments = {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"-{_decimal_wire(candidate.proposed_credit)}",
        "client_order_id": identifiers.client_order_id,
        "order_class": "mleg",
        "legs": [
            _wire_leg(candidate.long_put, side="buy", position_intent="buy_to_open"),
            _wire_leg(
                candidate.short_put, side="sell", position_intent="sell_to_open"
            ),
            _wire_leg(
                candidate.short_call, side="sell", position_intent="sell_to_open"
            ),
            _wire_leg(candidate.long_call, side="buy", position_intent="buy_to_open"),
        ],
    }
    validate_mleg_arguments(arguments, action="entry")
    return _freeze_intent(OrderPurpose.ENTRY, sequence, identifiers, arguments)


def build_exit_order_intent(
    candidate: CondorCandidate,
    *,
    limit_debit: Decimal,
    environment: str,
    account_id: str,
    event_date: date,
    strategy_version: str,
    sequence: int = 0,
) -> OrderIntent:
    """Build one atomic opposite-side close request.

    The requested debit must be a positive finite ``Decimal``.  It is capped
    at the actual symmetric wing width before being put on the wire.
    """

    expiration = _validate_candidate(candidate)
    debit = _require_decimal(limit_debit, "exit limit debit")
    if debit <= 0:
        raise PolicyError("exit limit debit must be positive")
    capped_debit = min(debit, candidate.wing_width)
    identifiers = derive_order_identifiers(
        environment=environment,
        account_id=account_id,
        event_date=event_date,
        strategy_version=strategy_version,
        symbol=candidate.symbol,
        expiration=expiration,
        purpose=OrderPurpose.EXIT,
        sequence=sequence,
    )
    arguments = {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": _decimal_wire(capped_debit),
        "client_order_id": identifiers.client_order_id,
        "order_class": "mleg",
        "legs": [
            _wire_leg(
                candidate.long_put, side="sell", position_intent="sell_to_close"
            ),
            _wire_leg(
                candidate.short_put, side="buy", position_intent="buy_to_close"
            ),
            _wire_leg(
                candidate.short_call, side="buy", position_intent="buy_to_close"
            ),
            _wire_leg(
                candidate.long_call, side="sell", position_intent="sell_to_close"
            ),
        ],
    }
    validate_mleg_arguments(arguments, action="exit")
    return _freeze_intent(OrderPurpose.EXIT, sequence, identifiers, arguments)


def build_exit_from_entry_arguments(
    entry_arguments: dict[str, Any],
    *,
    limit_debit: Decimal,
    environment: str,
    account_id: str,
    event_date: date,
    strategy_version: str,
    sequence: int = 0,
) -> OrderIntent:
    """Reconstruct an atomic exit from the durable entry payload.

    This is the restart-safe path used the morning after entry: it does not
    depend on retaining an in-memory candidate or asking the model to recreate
    option symbols.
    """

    validate_mleg_arguments(entry_arguments, action="entry")
    debit = _require_decimal(limit_debit, "exit limit debit")
    if debit <= 0:
        raise PolicyError("exit limit debit must be positive")
    legs = entry_arguments["legs"]
    long_put, short_put, short_call, long_call = legs
    symbols = [str(leg["symbol"]) for leg in legs]
    root = _occ_root(symbols[0])
    expiry = _occ_expiration(symbols[0])
    if any(_occ_root(symbol) != root or _occ_expiration(symbol) != expiry for symbol in symbols):
        raise PolicyError("entry MLEG symbols do not share a root and expiration")
    put_width = _occ_strike(short_put["symbol"]) - _occ_strike(long_put["symbol"])
    call_width = _occ_strike(long_call["symbol"]) - _occ_strike(short_call["symbol"])
    if put_width <= 0 or put_width != call_width:
        raise PolicyError("entry MLEG has invalid wing geometry")
    capped_debit = min(debit, put_width)
    identifiers = derive_order_identifiers(
        environment=environment,
        account_id=account_id,
        event_date=event_date,
        strategy_version=strategy_version,
        symbol=root,
        expiration=expiry,
        purpose=OrderPurpose.EXIT,
        sequence=sequence,
    )
    closing = {
        "buy": ("sell", "sell_to_close"),
        "sell": ("buy", "buy_to_close"),
    }
    arguments = {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": _decimal_wire(capped_debit),
        "client_order_id": identifiers.client_order_id,
        "order_class": "mleg",
        "legs": [
            {
                "symbol": leg["symbol"],
                "ratio_qty": "1",
                "side": closing[leg["side"]][0],
                "position_intent": closing[leg["side"]][1],
            }
            for leg in legs
        ],
    }
    validate_mleg_arguments(arguments, action="exit")
    return _freeze_intent(OrderPurpose.EXIT, sequence, identifiers, arguments)


def derive_order_identifiers(
    *,
    environment: str,
    account_id: str,
    event_date: date,
    strategy_version: str,
    symbol: str,
    expiration: date,
    purpose: OrderPurpose | Literal["entry", "exit"],
    sequence: int = 0,
) -> OrderIdentifiers:
    """Derive deterministic, account-separated IDs without exposing account ID."""

    environment_value = _require_text(environment, "environment")
    account_value = _require_text(account_id, "account ID")
    strategy_value = _require_text(strategy_version, "strategy version")
    symbol_value = _require_text(symbol, "symbol").upper()
    if not re.fullmatch(r"[A-Z0-9]{1,6}", symbol_value):
        raise PolicyError("symbol must be a valid OCC root")
    if isinstance(event_date, datetime) or not isinstance(event_date, date):
        raise PolicyError("event date must be a date")
    if isinstance(expiration, datetime) or not isinstance(expiration, date):
        raise PolicyError("expiration must be a date")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise PolicyError("order attempt sequence must be a non-negative integer")
    try:
        purpose_value = OrderPurpose(purpose)
    except ValueError as exc:
        raise PolicyError("order purpose must be entry or exit") from exc

    stable_identity = {
        "account_id": account_value,
        "environment": environment_value,
        "event_date": event_date.isoformat(),
        "expiration": expiration.isoformat(),
        "purpose": purpose_value.value,
        "strategy_version": strategy_value,
        "symbol": symbol_value,
    }
    base_digest = hashlib.sha256(_canonical_bytes(stable_identity)).hexdigest()[:24]
    attempt_digest = hashlib.sha256(
        _canonical_bytes({**stable_identity, "sequence": sequence})
    ).hexdigest()[:24]
    environment_slug = _slug(environment_value, maximum=8)
    purpose_slug = "e" if purpose_value is OrderPurpose.ENTRY else "x"
    date_slug = event_date.strftime("%Y%m%d")
    symbol_slug = symbol_value.lower()

    result = OrderIdentifiers(
        intent_id=f"tt-intent-{purpose_slug}-{date_slug}-{base_digest}",
        chain_id=f"tt-chain-{purpose_slug}-{date_slug}-{base_digest}",
        attempt_id=f"tt-attempt-{purpose_slug}-{sequence}-{attempt_digest}",
        client_order_id=(
            f"tt-{environment_slug}-{date_slug}-{symbol_slug}-"
            f"{purpose_slug}{sequence}-{attempt_digest[:20]}"
        ),
    )
    for field_name in ("intent_id", "chain_id", "attempt_id", "client_order_id"):
        identifier = getattr(result, field_name)
        if not identifier.startswith("tt-") or len(identifier) > _MAX_IDENTIFIER_LENGTH:
            raise PolicyError(f"generated {field_name} is outside identifier policy")
    return result


def serialize_candidate(candidate: CondorCandidate) -> dict[str, Any]:
    serialized = serialize_for_storage(candidate)
    if not isinstance(serialized, dict):  # pragma: no cover - type invariant
        raise TypeError("candidate serialization must be an object")
    return serialized


def serialize_evaluation(evaluation: StrategyEvaluation) -> dict[str, Any]:
    serialized = serialize_for_storage(evaluation)
    if not isinstance(serialized, dict):  # pragma: no cover - type invariant
        raise TypeError("evaluation serialization must be an object")
    return serialized


def serialize_for_storage(value: Any) -> Any:
    """Recursively convert domain dataclasses to JSON-safe exact primitives.

    Decimal values are strings, aware datetimes are normalized to UTC, enums
    use their wire values, and binary floats are rejected rather than silently
    entering strategy evidence.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: serialize_for_storage(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return serialize_for_storage(value.value)
    if isinstance(value, Decimal):
        return _decimal_wire(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("serialized datetimes must include a timezone")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): serialize_for_storage(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [serialize_for_storage(item) for item in value]
    if isinstance(value, float):
        raise TypeError("binary floats are prohibited in order evidence")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported order-evidence value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON after exact primitive conversion."""

    return json.dumps(
        serialize_for_storage(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _occ_match(symbol: str) -> re.Match[str]:
    match = re.fullmatch(
        r"(?P<root>[A-Z0-9]{1,6})(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d{8})",
        str(symbol),
    )
    if match is None:
        raise PolicyError("entry contains an invalid OCC symbol")
    return match


def _occ_root(symbol: str) -> str:
    return _occ_match(symbol).group("root")


def _occ_expiration(symbol: str) -> date:
    return datetime.strptime(_occ_match(symbol).group("expiry"), "%y%m%d").date()


def _occ_strike(symbol: str) -> Decimal:
    return Decimal(_occ_match(symbol).group("strike")) / Decimal("1000")


def _validate_candidate(candidate: CondorCandidate) -> date:
    if not isinstance(candidate, CondorCandidate):
        raise TypeError("candidate must be a CondorCandidate")
    if candidate.quantity != 1:
        raise PolicyError("order intent requires candidate quantity exactly one")
    symbol = _require_text(candidate.symbol, "candidate symbol").upper()
    if symbol != candidate.symbol or not re.fullmatch(r"[A-Z0-9]{1,6}", symbol):
        raise PolicyError("candidate symbol must be an uppercase OCC root")

    expected: tuple[tuple[StrategyLeg, LegRole, OrderSide, OptionRight], ...] = (
        (candidate.long_put, LegRole.LONG_PUT, OrderSide.BUY, OptionRight.PUT),
        (candidate.short_put, LegRole.SHORT_PUT, OrderSide.SELL, OptionRight.PUT),
        (candidate.short_call, LegRole.SHORT_CALL, OrderSide.SELL, OptionRight.CALL),
        (candidate.long_call, LegRole.LONG_CALL, OrderSide.BUY, OptionRight.CALL),
    )
    expirations: set[date] = set()
    symbols: set[str] = set()
    for leg, role, side, right in expected:
        if leg.role is not role or leg.side is not side:
            raise PolicyError("candidate leg role or entry side was tampered")
        contract = leg.snapshot.contract
        if contract.right is not right:
            raise PolicyError("candidate option right does not match its leg role")
        if contract.underlying_symbol.upper() != symbol:
            raise PolicyError("candidate leg belongs to another underlying")
        if not contract.tradable or contract.status != "active" or not contract.is_standard:
            raise PolicyError("candidate contains an ineligible option contract")
        expirations.add(contract.expiration)
        symbols.add(contract.symbol)
    if len(expirations) != 1 or len(symbols) != 4:
        raise PolicyError("candidate legs must have one expiration and unique symbols")

    long_put_strike = _require_decimal(
        candidate.long_put.snapshot.contract.strike, "long put strike"
    )
    short_put_strike = _require_decimal(
        candidate.short_put.snapshot.contract.strike, "short put strike"
    )
    short_call_strike = _require_decimal(
        candidate.short_call.snapshot.contract.strike, "short call strike"
    )
    long_call_strike = _require_decimal(
        candidate.long_call.snapshot.contract.strike, "long call strike"
    )
    if not long_put_strike < short_put_strike < short_call_strike < long_call_strike:
        raise PolicyError("candidate strikes do not form an iron condor")
    put_width = short_put_strike - long_put_strike
    call_width = long_call_strike - short_call_strike
    wing_width = _require_decimal(candidate.wing_width, "candidate wing width")
    if put_width != call_width or wing_width != put_width or wing_width > Decimal("5"):
        raise PolicyError("candidate does not have equal policy-bounded wing widths")

    credit = _require_decimal(candidate.proposed_credit, "candidate proposed credit")
    tick = _require_decimal(candidate.tick_size, "candidate tick size")
    if credit <= 0 or tick <= 0 or credit % tick != 0:
        raise PolicyError("candidate credit must be positive and tick aligned")
    expected_profit = credit * _HUNDRED
    expected_loss = (wing_width - credit) * _HUNDRED
    maximum_profit = _require_decimal(
        candidate.maximum_profit, "candidate maximum profit"
    )
    maximum_loss = _require_decimal(candidate.maximum_loss, "candidate maximum loss")
    risk_budget = _require_decimal(candidate.risk_budget, "candidate risk budget")
    if (
        maximum_profit != expected_profit
        or maximum_loss != expected_loss
        or expected_loss <= 0
        or expected_loss > Decimal("500")
        or expected_loss > risk_budget
    ):
        raise PolicyError("candidate credit and risk economics are inconsistent")
    return next(iter(expirations))


def _wire_leg(
    leg: StrategyLeg,
    *,
    side: Literal["buy", "sell"],
    position_intent: Literal[
        "buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"
    ],
) -> dict[str, str]:
    return {
        "symbol": leg.snapshot.contract.symbol,
        "ratio_qty": "1",
        "side": side,
        "position_intent": position_intent,
    }


def _freeze_intent(
    purpose: OrderPurpose,
    sequence: int,
    identifiers: OrderIdentifiers,
    arguments: dict[str, Any],
) -> OrderIntent:
    arguments_json = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return OrderIntent(
        purpose=purpose,
        sequence=sequence,
        identifiers=identifiers,
        arguments_json=arguments_json,
        arguments_hash=payload_hash(arguments),
    )


def _require_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{field_name} must be a finite Decimal")
    return value


def _decimal_wire(value: Decimal) -> str:
    exact = _require_decimal(value, "decimal value")
    return format(exact, "f")


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{field_name} must be a non-empty string")
    return value.strip()


def _slug(value: str, *, maximum: int) -> str:
    slug = _SLUG_CHARACTER.sub("-", value.lower()).strip("-")[:maximum].strip("-")
    return slug or "env"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
