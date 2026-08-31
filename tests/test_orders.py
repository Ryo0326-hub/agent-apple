from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from thetatrap.domain import (
    CondorCandidate,
    GateCode,
    GateFailure,
    LegRole,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionSnapshot,
    OrderSide,
    StrategyEvaluation,
    StrategyLeg,
)
from thetatrap.errors import PolicyError
from thetatrap.orders import (
    OrderPurpose,
    build_entry_order_intent,
    build_exit_order_intent,
    build_exit_from_entry_arguments,
    canonical_json,
    derive_order_identifiers,
    serialize_candidate,
    serialize_evaluation,
    serialize_for_storage,
)
from thetatrap.policy import exact_arguments, payload_hash, validate_mleg_arguments


D = Decimal
OBSERVED_AT = datetime(2026, 9, 1, 19, 30, tzinfo=UTC)
EVENT_DATE = date(2026, 9, 1)
EXPIRATION = date(2026, 9, 4)


def _snapshot(
    symbol: str,
    strike: str,
    right: OptionRight,
    *,
    bid: str,
    ask: str,
    delta: str,
) -> OptionSnapshot:
    return OptionSnapshot(
        contract=OptionContract(
            symbol=symbol,
            underlying_symbol="TEST",
            expiration=EXPIRATION,
            right=right,
            strike=D(strike),
            tradable=True,
            status="active",
            multiplier=D("100"),
            size=D("100"),
            open_interest=100,
            open_interest_date=date(2026, 8, 31),
            ppind=True,
        ),
        quote=OptionQuote(
            bid=D(bid),
            ask=D(ask),
            timestamp=OBSERVED_AT,
            implied_volatility=D("0.60"),
            delta=D(delta),
        ),
    )


def candidate() -> CondorCandidate:
    long_put = StrategyLeg(
        role=LegRole.LONG_PUT,
        side=OrderSide.BUY,
        snapshot=_snapshot(
            "TEST260904P00090000", "90", OptionRight.PUT, bid="0.20", ask="0.25", delta="-0.08"
        ),
    )
    short_put = StrategyLeg(
        role=LegRole.SHORT_PUT,
        side=OrderSide.SELL,
        snapshot=_snapshot(
            "TEST260904P00095000", "95", OptionRight.PUT, bid="1.40", ask="1.50", delta="-0.18"
        ),
    )
    short_call = StrategyLeg(
        role=LegRole.SHORT_CALL,
        side=OrderSide.SELL,
        snapshot=_snapshot(
            "TEST260904C00105000", "105", OptionRight.CALL, bid="1.40", ask="1.50", delta="0.18"
        ),
    )
    long_call = StrategyLeg(
        role=LegRole.LONG_CALL,
        side=OrderSide.BUY,
        snapshot=_snapshot(
            "TEST260904C00110000", "110", OptionRight.CALL, bid="0.20", ask="0.25", delta="0.08"
        ),
    )
    return CondorCandidate(
        symbol="TEST",
        observed_at=OBSERVED_AT,
        spot=D("100.00"),
        front_atm_strike=D("100"),
        back_atm_strike=D("100"),
        expected_move=D("5.00"),
        expected_move_fraction=D("0.05"),
        front_atm_iv=D("0.60"),
        back_atm_iv=D("0.50"),
        iv_ratio=D("1.20"),
        long_put=long_put,
        short_put=short_put,
        short_call=short_call,
        long_call=long_call,
        wing_width=D("5"),
        natural_credit=D("2.30"),
        midpoint_credit=D("2.450"),
        proposed_credit=D("2.45"),
        tick_size=D("0.01"),
        maximum_profit=D("245.00"),
        maximum_loss=D("255.00"),
        risk_budget=D("500"),
        quantity=1,
        aggregate_relative_spread=D("0.30"),
        net_delta=D("0.00"),
    )


def _entry(**changes: Any):
    values = {
        "environment": "competition",
        "account_id": "9b99b86e-5f65-4e7a-b35f-secret-account",
        "event_date": EVENT_DATE,
        "strategy_version": "thetatrap-v1.1",
    }
    values.update(changes)
    return build_entry_order_intent(candidate(), **values)


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_float(item) for item in value)
    return False


def test_entry_builder_emits_exact_native_mleg_and_passes_frozen_policy() -> None:
    intent = _entry()
    arguments = intent.arguments

    assert arguments == {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "-2.45",
        "client_order_id": intent.client_order_id,
        "order_class": "mleg",
        "legs": [
            {
                "symbol": "TEST260904P00090000",
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
            {
                "symbol": "TEST260904P00095000",
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": "TEST260904C00105000",
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": "TEST260904C00110000",
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
        ],
    }
    assert isinstance(arguments["legs"], list)
    validate_mleg_arguments(arguments, action="entry")
    assert intent.arguments_hash == payload_hash(arguments)
    assert not _contains_float(intent.as_record())


def test_exit_builder_is_atomic_opposite_side_and_caps_debit_at_wing_width() -> None:
    intent = build_exit_order_intent(
        candidate(),
        limit_debit=D("99.99"),
        environment="competition",
        account_id="account-secret",
        event_date=EVENT_DATE,
        strategy_version="thetatrap-v1.1",
    )
    arguments = intent.arguments

    assert arguments["limit_price"] == "5"
    assert [(leg["side"], leg["position_intent"]) for leg in arguments["legs"]] == [
        ("sell", "sell_to_close"),
        ("buy", "buy_to_close"),
        ("buy", "buy_to_close"),
        ("sell", "sell_to_close"),
    ]
    validate_mleg_arguments(arguments, action="exit")
    assert intent.purpose is OrderPurpose.EXIT


def test_restart_safe_exit_rebuilds_only_from_immutable_entry() -> None:
    entry = _entry()
    exit_intent = build_exit_from_entry_arguments(
        entry.arguments,
        limit_debit=D("1.35"),
        environment="competition",
        account_id="account-secret",
        event_date=EVENT_DATE,
        strategy_version="thetatrap-v1.1",
    )
    assert exit_intent.arguments["limit_price"] == "1.35"
    assert [leg["symbol"] for leg in exit_intent.arguments["legs"]] == [
        leg["symbol"] for leg in entry.arguments["legs"]
    ]
    assert [(leg["side"], leg["position_intent"]) for leg in exit_intent.arguments["legs"]] == [
        ("sell", "sell_to_close"),
        ("buy", "buy_to_close"),
        ("buy", "buy_to_close"),
        ("sell", "sell_to_close"),
    ]
    validate_mleg_arguments(exit_intent.arguments, action="exit")


@pytest.mark.parametrize("bad_debit", [D("0"), D("-0.01")])
def test_exit_builder_rejects_nonpositive_debit(bad_debit: Decimal) -> None:
    with pytest.raises(PolicyError, match="positive"):
        build_exit_order_intent(
            candidate(),
            limit_debit=bad_debit,
            environment="dev",
            account_id="account",
            event_date=EVENT_DATE,
            strategy_version="v1",
        )


@pytest.mark.parametrize("bad_debit", [1.25, D("NaN"), D("Infinity")])
def test_exit_builder_rejects_float_or_nonfinite_debit(bad_debit: Any) -> None:
    with pytest.raises(TypeError, match="finite Decimal"):
        build_exit_order_intent(
            candidate(),
            limit_debit=bad_debit,
            environment="dev",
            account_id="account",
            event_date=EVENT_DATE,
            strategy_version="v1",
        )


def test_identifiers_are_deterministic_bounded_and_do_not_leak_account() -> None:
    account_id = "PA-THIS-IS-THE-FULL-PRIVATE-ACCOUNT-ID"
    kwargs = {
        "environment": "competition",
        "account_id": account_id,
        "event_date": EVENT_DATE,
        "strategy_version": "thetatrap-v1.1",
        "symbol": "TEST",
        "expiration": EXPIRATION,
        "purpose": OrderPurpose.ENTRY,
        "sequence": 0,
    }
    first = derive_order_identifiers(**kwargs)
    second = derive_order_identifiers(**kwargs)

    assert first == second
    for identifier in (
        first.intent_id,
        first.chain_id,
        first.attempt_id,
        first.client_order_id,
    ):
        assert identifier.startswith("tt-")
        assert len(identifier) <= 128
        assert account_id not in identifier


def test_account_purpose_and_retry_sequence_have_collision_resistant_separation() -> None:
    base = {
        "environment": "development",
        "account_id": "account-a",
        "event_date": EVENT_DATE,
        "strategy_version": "v1",
        "symbol": "TEST",
        "expiration": EXPIRATION,
        "purpose": "entry",
    }
    initial = derive_order_identifiers(**base, sequence=0)
    retry = derive_order_identifiers(**base, sequence=1)
    another_account = derive_order_identifiers(
        **{**base, "account_id": "account-b"}, sequence=0
    )
    exit_ids = derive_order_identifiers(**{**base, "purpose": "exit"}, sequence=0)

    assert initial.intent_id == retry.intent_id
    assert initial.chain_id == retry.chain_id
    assert initial.attempt_id != retry.attempt_id
    assert initial.client_order_id != retry.client_order_id
    assert initial != another_account
    assert initial != exit_ids


def test_arguments_property_returns_copy_and_tamper_fails_exact_match() -> None:
    intent = _entry()
    changed = intent.arguments
    changed["limit_price"] = "-2.40"
    changed["legs"][0]["side"] = "sell"

    assert intent.arguments["limit_price"] == "-2.45"
    assert intent.arguments["legs"][0]["side"] == "buy"
    with pytest.raises(PolicyError, match="differ"):
        exact_arguments(intent.arguments, changed)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: replace(value, quantity=2), "quantity"),
        (lambda value: replace(value, wing_width=D("4")), "wing widths"),
        (lambda value: replace(value, maximum_loss=D("1")), "economics"),
        (
            lambda value: replace(
                value,
                long_put=replace(value.long_put, role=LegRole.SHORT_PUT),
            ),
            "tampered",
        ),
        (
            lambda value: replace(
                value,
                long_call=replace(
                    value.long_call,
                    snapshot=replace(
                        value.long_call.snapshot,
                        contract=replace(
                            value.long_call.snapshot.contract,
                            symbol=value.short_call.snapshot.contract.symbol,
                        ),
                    ),
                ),
            ),
            "unique",
        ),
    ],
)
def test_entry_builder_rejects_tampered_candidate(mutation: Any, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        build_entry_order_intent(
            mutation(candidate()),
            environment="dev",
            account_id="account",
            event_date=EVENT_DATE,
            strategy_version="v1",
        )


def test_candidate_and_evaluation_serialization_is_decimal_datetime_enum_safe() -> None:
    approved = candidate()
    candidate_payload = serialize_candidate(approved)
    evaluation_payload = serialize_evaluation(
        StrategyEvaluation(symbol="TEST", candidate=approved)
    )
    rejected_payload = serialize_evaluation(
        StrategyEvaluation(
            symbol="TEST",
            candidate=None,
            failures=(GateFailure(GateCode.IV_RATIO_LOW, "below threshold"),),
        )
    )

    assert candidate_payload["proposed_credit"] == "2.45"
    assert candidate_payload["observed_at"] == "2026-09-01T19:30:00Z"
    assert candidate_payload["long_put"]["role"] == "long_put"
    assert candidate_payload["long_put"]["snapshot"]["contract"]["expiration"] == "2026-09-04"
    assert evaluation_payload["candidate"] == candidate_payload
    assert rejected_payload["failures"] == [
        {"code": "IV_RATIO_LOW", "detail": "below threshold"}
    ]
    assert not _contains_float(candidate_payload)
    json.dumps(evaluation_payload)


def test_canonical_json_is_key_stable_and_rejects_binary_float() -> None:
    assert canonical_json({"z": D("1.20"), "a": EVENT_DATE}) == (
        '{"a":"2026-09-01","z":"1.20"}'
    )
    with pytest.raises(TypeError, match="floats"):
        serialize_for_storage({"unsafe": 1.2})


@dataclass(frozen=True)
class _TimestampEvidence:
    observed_at: datetime


def test_serialization_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone"):
        serialize_for_storage(_TimestampEvidence(datetime(2026, 9, 1, 12, 0)))
