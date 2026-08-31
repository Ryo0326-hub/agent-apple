"""Deterministic authorization for the narrow paper-trading mutation surface."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from thetatrap.errors import PolicyError


OCC_SYMBOL = re.compile(
    r"^(?P<root>[A-Z0-9]{1,6})(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d{8})$"
)
ENTRY_KEYS = frozenset(
    {
        "qty",
        "type",
        "time_in_force",
        "limit_price",
        "client_order_id",
        "order_class",
        "legs",
    }
)
LEG_KEYS = frozenset({"symbol", "ratio_qty", "side", "position_intent"})
SYSTEM_MUTATION_PURPOSES = frozenset(
    {"reprice", "cancel", "exit", "kill_switch"}
)


class MutationPurpose(StrEnum):
    ENTRY = "entry"
    REPRICE = "reprice"
    CANCEL = "cancel"
    EXIT = "exit"
    KILL_SWITCH = "kill_switch"


@dataclass(frozen=True)
class MutationPermit:
    """Short-lived, exact-payload capability checked immediately before MCP dispatch."""

    permit_id: str
    tool_name: str
    principal: Literal["agent", "system"]
    purpose: MutationPurpose
    arguments_hash: str
    intent_id: str
    expires_at: datetime

    def assert_call(
        self,
        *,
        tool_name: str,
        principal: str,
        arguments: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        observed_at = now or datetime.now(UTC)
        if observed_at.tzinfo is None:
            raise PolicyError("mutation permit validation requires a timezone-aware time")
        if observed_at > self.expires_at:
            raise PolicyError("mutation permit expired")
        if tool_name != self.tool_name or principal != self.principal:
            raise PolicyError("mutation call does not match the authorized tool principal")
        if payload_hash(arguments) != self.arguments_hash:
            raise PolicyError("mutation arguments do not exactly match the authorized payload")


def payload_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def exact_arguments(expected: dict[str, Any], observed: dict[str, Any]) -> None:
    if payload_hash(expected) != payload_hash(observed):
        raise PolicyError("model mutation arguments differ from the immutable order intent")


def normalize_exact_model_arguments(
    expected: dict[str, Any], observed: dict[str, Any]
) -> dict[str, Any]:
    """Decode only Qwen's known stringified-leg quirk, then require exact equality.

    No value is inferred or repaired. Only ``legs`` may be decoded from JSON,
    and the resulting native list must equal the durable authorized list.
    """

    if not isinstance(observed, dict):
        raise PolicyError("model mutation arguments must be an object")
    normalized = deepcopy(observed)
    legs = normalized.get("legs")
    if isinstance(legs, str):
        try:
            decoded = json.loads(legs)
        except json.JSONDecodeError as exc:
            raise PolicyError("model emitted invalid JSON for MLEG legs") from exc
        if not isinstance(decoded, list):
            raise PolicyError("model MLEG legs JSON must decode to an array")
        normalized["legs"] = decoded
    exact_arguments(expected, normalized)
    return normalized


def make_entry_permit(
    *,
    intent_id: str,
    arguments: dict[str, Any],
    now: datetime | None = None,
    ttl_seconds: int = 60,
) -> MutationPermit:
    validate_mleg_arguments(arguments, action="entry")
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise PolicyError("mutation permits require a timezone-aware issue time")
    return MutationPermit(
        permit_id=str(uuid.uuid4()),
        tool_name="place_option_order",
        principal="agent",
        purpose=MutationPurpose.ENTRY,
        arguments_hash=payload_hash(arguments),
        intent_id=intent_id,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
    )


def make_system_permit(
    *,
    tool_name: str,
    purpose: MutationPurpose,
    intent_id: str,
    arguments: dict[str, Any],
    now: datetime | None = None,
    ttl_seconds: int = 60,
) -> MutationPermit:
    if purpose.value not in SYSTEM_MUTATION_PURPOSES:
        raise PolicyError("system permit purpose is not lifecycle-reducing")
    if tool_name == "place_option_order":
        validate_mleg_arguments(arguments, action="exit")
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise PolicyError("mutation permits require a timezone-aware issue time")
    return MutationPermit(
        permit_id=str(uuid.uuid4()),
        tool_name=tool_name,
        principal="system",
        purpose=purpose,
        arguments_hash=payload_hash(arguments),
        intent_id=intent_id,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
    )


def validate_mleg_arguments(
    arguments: dict[str, Any], *, action: Literal["entry", "exit"]
) -> None:
    if set(arguments) != ENTRY_KEYS:
        raise PolicyError("MLEG payload must contain only the frozen order-intent fields")
    if arguments.get("qty") != "1":
        raise PolicyError("ThetaTrap permits exactly one strategy contract")
    if arguments.get("type") != "limit" or arguments.get("time_in_force") != "day":
        raise PolicyError("ThetaTrap permits DAY limit option orders only")
    if arguments.get("order_class") != "mleg":
        raise PolicyError("ThetaTrap option orders must use order_class=mleg")
    client_order_id = arguments.get("client_order_id")
    if (
        not isinstance(client_order_id, str)
        or not client_order_id.startswith("tt-")
        or len(client_order_id) > 128
    ):
        raise PolicyError("invalid deterministic ThetaTrap client order ID")

    legs = arguments.get("legs")
    if not isinstance(legs, list) or len(legs) != 4:
        raise PolicyError("ThetaTrap MLEG orders require four native leg objects")
    if any(not isinstance(leg, dict) or set(leg) != LEG_KEYS for leg in legs):
        raise PolicyError("each MLEG leg must contain the exact approved fields")

    parsed: list[dict[str, Any]] = []
    for leg in legs:
        if leg.get("ratio_qty") != "1":
            raise PolicyError("all iron-condor leg ratios must equal one")
        if leg.get("side") not in {"buy", "sell"}:
            raise PolicyError("invalid MLEG side")
        match = OCC_SYMBOL.fullmatch(str(leg.get("symbol") or ""))
        if match is None:
            raise PolicyError("invalid OCC option symbol in MLEG intent")
        parsed.append(
            {
                **leg,
                "root": match.group("root"),
                "expiry": match.group("expiry"),
                "right": match.group("right"),
                "strike": Decimal(match.group("strike")) / Decimal("1000"),
            }
        )

    if len({item["symbol"] for item in parsed}) != 4:
        raise PolicyError("MLEG option symbols must be unique")
    if len({(item["root"], item["expiry"]) for item in parsed}) != 1:
        raise PolicyError("all MLEG legs must share one root and expiration")

    puts = sorted((item for item in parsed if item["right"] == "P"), key=lambda x: x["strike"])
    calls = sorted((item for item in parsed if item["right"] == "C"), key=lambda x: x["strike"])
    if len(puts) != 2 or len(calls) != 2:
        raise PolicyError("iron condor requires exactly two puts and two calls")
    if puts[1]["strike"] >= calls[0]["strike"]:
        raise PolicyError("iron-condor short put must be below the short call")
    put_width = puts[1]["strike"] - puts[0]["strike"]
    call_width = calls[1]["strike"] - calls[0]["strike"]
    if put_width <= 0 or put_width != call_width or put_width > Decimal("5"):
        raise PolicyError("iron-condor wings must be equal and no wider than five dollars")

    if action == "entry":
        expected = {
            puts[0]["symbol"]: ("buy", "buy_to_open"),
            puts[1]["symbol"]: ("sell", "sell_to_open"),
            calls[0]["symbol"]: ("sell", "sell_to_open"),
            calls[1]["symbol"]: ("buy", "buy_to_open"),
        }
    else:
        expected = {
            puts[0]["symbol"]: ("sell", "sell_to_close"),
            puts[1]["symbol"]: ("buy", "buy_to_close"),
            calls[0]["symbol"]: ("buy", "buy_to_close"),
            calls[1]["symbol"]: ("sell", "sell_to_close"),
        }
    for item in parsed:
        if (item["side"], item["position_intent"]) != expected[item["symbol"]]:
            raise PolicyError(f"invalid {action} side or position intent for iron-condor leg")

    try:
        signed_price = Decimal(str(arguments.get("limit_price")))
    except (InvalidOperation, TypeError) as exc:
        raise PolicyError("MLEG limit price must be a decimal string") from exc
    if not signed_price.is_finite():
        raise PolicyError("MLEG limit price must be finite")
    if action == "entry":
        credit = -signed_price
        minimum_credit = max(Decimal("0.20"), put_width * Decimal("0.10"))
        if signed_price >= 0 or credit < minimum_credit or credit >= put_width:
            raise PolicyError("entry credit is outside the bounded policy range")
        if (put_width - credit) * Decimal("100") > Decimal("500"):
            raise PolicyError("entry maximum loss exceeds five hundred dollars")
    elif signed_price <= 0 or signed_price > put_width:
        raise PolicyError("exit debit must be positive and no greater than wing width")
