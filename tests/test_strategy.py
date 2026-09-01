from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from thetatrap.domain import (
    GateCode,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionSnapshot,
    UnderlyingQuote,
)
from thetatrap.strategy import (
    StrategyConfig,
    evaluate_symbol,
    option_tick_size,
    rank_candidates,
    round_credit_down,
    round_debit_up,
    select_atm_pair,
)


D = Decimal
OBSERVED_AT = datetime(2026, 9, 1, 19, 30, tzinfo=UTC)
TRADE_EXPIRATION = date(2026, 9, 4)
TERM_EXPIRATION = date(2026, 9, 11)
PREVIOUS_TRADING_DAY = date(2026, 8, 31)


def option(
    strike: str,
    right: OptionRight,
    bid: str,
    ask: str,
    *,
    expiration: date = TRADE_EXPIRATION,
    iv: str | None = "0.60",
    delta: str | None = None,
    open_interest: int | None = 100,
    open_interest_date: date | None = PREVIOUS_TRADING_DAY,
    ppind: bool | None = True,
    timestamp: datetime = OBSERVED_AT - timedelta(seconds=5),
    underlying_symbol: str = "TEST",
) -> OptionSnapshot:
    strike_decimal = D(strike)
    type_letter = "C" if right is OptionRight.CALL else "P"
    symbol = (
        f"{underlying_symbol}260904{type_letter}{str(strike_decimal).replace('.', '')}"
    )
    return OptionSnapshot(
        contract=OptionContract(
            symbol=symbol,
            underlying_symbol=underlying_symbol,
            expiration=expiration,
            right=right,
            strike=strike_decimal,
            tradable=True,
            status="active",
            multiplier=D("100"),
            size=D("100"),
            open_interest=open_interest,
            open_interest_date=open_interest_date,
            ppind=ppind,
        ),
        quote=OptionQuote(
            bid=D(bid),
            ask=D(ask),
            timestamp=timestamp,
            implied_volatility=D(iv) if iv is not None else None,
            delta=D(delta) if delta is not None else None,
        ),
    )


def valid_chains() -> tuple[list[OptionSnapshot], list[OptionSnapshot]]:
    front = [
        option("90", OptionRight.PUT, "0.20", "0.25", delta="-0.08"),
        option("95", OptionRight.PUT, "1.40", "1.50", delta="-0.18"),
        option("100", OptionRight.PUT, "2.40", "2.60", delta="-0.50"),
        option("100", OptionRight.CALL, "2.40", "2.60", delta="0.50"),
        option("105", OptionRight.CALL, "1.40", "1.50", delta="0.18"),
        option("110", OptionRight.CALL, "0.20", "0.25", delta="0.08"),
    ]
    back = [
        option(
            "100",
            OptionRight.PUT,
            "4.90",
            "5.10",
            expiration=TERM_EXPIRATION,
            iv="0.50",
            delta="-0.50",
        ),
        option(
            "100",
            OptionRight.CALL,
            "4.90",
            "5.10",
            expiration=TERM_EXPIRATION,
            iv="0.50",
            delta="0.50",
        ),
    ]
    return front, back


def evaluation_inputs() -> dict[str, object]:
    front, back = valid_chains()
    return {
        "symbol": "TEST",
        "observed_at": OBSERVED_AT,
        "underlying": UnderlyingQuote(
            bid=D("99.99"),
            ask=D("100.01"),
            timestamp=OBSERVED_AT - timedelta(seconds=2),
        ),
        "front_chain": front,
        "back_chain": back,
        "trade_expiration": TRADE_EXPIRATION,
        "term_expiration": TERM_EXPIRATION,
        "previous_trading_day": PREVIOUS_TRADING_DAY,
        "initial_equity": D("100000"),
        "buying_power": D("100000"),
    }


def mutate_quote(
    chain: list[OptionSnapshot],
    right: OptionRight,
    strike: str,
    **changes: object,
) -> list[OptionSnapshot]:
    target = D(strike)
    return [
        replace(snapshot, quote=replace(snapshot.quote, **changes))
        if snapshot.contract.right is right and snapshot.contract.strike == target
        else snapshot
        for snapshot in chain
    ]


def mutate_contract(
    chain: list[OptionSnapshot],
    right: OptionRight,
    strike: str,
    **changes: object,
) -> list[OptionSnapshot]:
    target = D(strike)
    return [
        replace(snapshot, contract=replace(snapshot.contract, **changes))
        if snapshot.contract.right is right and snapshot.contract.strike == target
        else snapshot
        for snapshot in chain
    ]


def test_mcp_like_contract_and_quote_payloads_normalize_to_decimal() -> None:
    snapshot = OptionSnapshot.from_mappings(
        {
            "symbol": "TEST260904P00095000",
            "underlying_symbol": "TEST",
            "expiration_date": "2026-09-04",
            "type": "put",
            "strike_price": "95.00",
            "tradable": True,
            "status": "ACTIVE",
            "multiplier": "100",
            "size": "100",
            "open_interest": "52",
            "open_interest_date": "2026-08-31",
            "ppind": "true",
            "deliverables": [],
        },
        {
            "bp": 1.2,
            "ap": "1.30",
            "t": "2026-09-01T19:29:55Z",
            "impliedVolatility": "0.55",
            "greeks": {"delta": "-0.20"},
        },
    )

    assert snapshot.contract.strike == D("95.00")
    assert snapshot.contract.open_interest == 52
    assert snapshot.contract.ppind is True
    assert snapshot.contract.is_standard is True
    assert snapshot.quote.bid == D("1.2")
    assert snapshot.quote.implied_volatility == D("0.55")
    assert snapshot.quote.delta == D("-0.20")
    assert snapshot.quote.timestamp.tzinfo is UTC


def test_deliverables_distinguish_standard_equity_from_adjusted_contract() -> None:
    base = {
        "symbol": "TEST260904P00095000",
        "underlying_symbol": "TEST",
        "expiration_date": "2026-09-04",
        "type": "put",
        "strike_price": "95",
        "tradable": True,
        "status": "active",
        "multiplier": "100",
        "size": "100",
    }
    standard = OptionContract.from_mapping(
        {
            **base,
            "deliverables": [
                {
                    "type": "equity",
                    "symbol": "TEST",
                    "amount": "100",
                    "allocation_percentage": "100",
                    "delayed_settlement": False,
                }
            ],
        }
    )
    adjusted = OptionContract.from_mapping(
        {
            **base,
            "deliverables": [
                {
                    "type": "cash",
                    "symbol": "TEST",
                    "amount": "100",
                    "allocation_percentage": "100",
                    "delayed_settlement": False,
                }
            ],
        }
    )
    assert standard.is_standard is True
    assert adjusted.is_standard is False


def test_naive_quote_timestamp_is_rejected_at_normalization_boundary() -> None:
    with pytest.raises(ValueError, match="timezone"):
        UnderlyingQuote.from_mapping(
            {"bid": "99", "ask": "100", "timestamp": "2026-09-01T15:30:00"}
        )


def test_atm_tie_uses_lower_strike_and_is_input_order_independent() -> None:
    chain = [
        option("101", OptionRight.CALL, "2", "2.10"),
        option("99", OptionRight.PUT, "2", "2.10"),
        option("101", OptionRight.PUT, "2", "2.10"),
        option("99", OptionRight.CALL, "2", "2.10"),
    ]
    pair = select_atm_pair(list(reversed(chain)), D("100"), TRADE_EXPIRATION)
    assert pair is not None
    assert pair.strike == D("99")


@pytest.mark.parametrize(
    ("price", "ppind", "expected_tick", "expected_down", "expected_up"),
    [
        ("2.678", True, "0.01", "2.67", "2.68"),
        ("3.04", True, "0.05", "3.00", "3.05"),
        ("2.68", False, "0.05", "2.65", "2.70"),
        ("2.68", None, "0.05", "2.65", "2.70"),
        ("3.04", False, "0.10", "3.00", "3.10"),
    ],
)
def test_ppind_aware_tick_rounding(
    price: str,
    ppind: bool | None,
    expected_tick: str,
    expected_down: str,
    expected_up: str,
) -> None:
    assert option_tick_size(D(price), ppind) == D(expected_tick)
    assert round_credit_down(D(price), ppind) == (D(expected_down), D(expected_tick))
    assert round_debit_up(D(price), ppind) == (D(expected_up), D(expected_tick))


def test_valid_candidate_has_expected_structure_credit_and_risk() -> None:
    result = evaluate_symbol(**evaluation_inputs())  # type: ignore[arg-type]

    assert result.eligible is True
    candidate = result.candidate
    assert candidate is not None
    assert candidate.spot == D("100.00")
    assert candidate.front_atm_strike == D("100")
    assert candidate.expected_move == D("5.00")
    assert candidate.expected_move_fraction == D("0.05")
    assert candidate.iv_ratio == D("1.2")
    assert candidate.short_put.snapshot.contract.strike == D("95")
    assert candidate.short_call.snapshot.contract.strike == D("105")
    assert candidate.long_put.snapshot.contract.strike == D("90")
    assert candidate.long_call.snapshot.contract.strike == D("110")
    assert candidate.wing_width == D("5")
    assert candidate.natural_credit == D("2.30")
    assert candidate.midpoint_credit == D("2.450")
    assert candidate.proposed_credit == D("2.45")
    assert candidate.tick_size == D("0.01")
    assert candidate.maximum_profit == D("245.00")
    assert candidate.maximum_loss == D("255.00")
    assert candidate.risk_budget == D("500")
    assert candidate.quantity == 1
    assert candidate.net_delta == D("0.00")


def test_candidate_is_identical_when_chain_order_changes() -> None:
    inputs = evaluation_inputs()
    inputs["front_chain"] = list(reversed(inputs["front_chain"]))  # type: ignore[arg-type]
    inputs["back_chain"] = list(reversed(inputs["back_chain"]))  # type: ignore[arg-type]
    candidate = evaluate_symbol(**inputs).candidate  # type: ignore[arg-type]
    assert candidate is not None
    assert [leg.snapshot.contract.strike for leg in candidate.legs] == [
        D("90"),
        D("95"),
        D("105"),
        D("110"),
    ]


def test_narrowest_valid_symmetric_wings_are_selected() -> None:
    inputs = evaluation_inputs()
    front = list(inputs["front_chain"])  # type: ignore[arg-type]
    front.extend(
        [
            option("92.5", OptionRight.PUT, "0.55", "0.60", delta="-0.12"),
            option("107.5", OptionRight.CALL, "0.55", "0.60", delta="0.12"),
        ]
    )
    inputs["front_chain"] = front

    candidate = evaluate_symbol(**inputs).candidate  # type: ignore[arg-type]
    assert candidate is not None
    assert candidate.wing_width == D("2.5")
    assert candidate.long_put.snapshot.contract.strike == D("92.5")
    assert candidate.long_call.snapshot.contract.strike == D("107.5")


def test_invalid_narrow_wings_fall_back_to_next_symmetric_width() -> None:
    inputs = evaluation_inputs()
    front = list(inputs["front_chain"])  # type: ignore[arg-type]
    front.extend(
        [
            option(
                "92.5",
                OptionRight.PUT,
                "0.55",
                "0.60",
                delta="-0.12",
                open_interest=9,
            ),
            option(
                "107.5",
                OptionRight.CALL,
                "0.55",
                "0.60",
                delta="0.12",
                open_interest=9,
            ),
        ]
    )
    inputs["front_chain"] = front

    candidate = evaluate_symbol(**inputs).candidate  # type: ignore[arg-type]
    assert candidate is not None
    assert candidate.wing_width == D("5")


def test_basic_profile_searches_outward_when_nearest_short_lacks_oi() -> None:
    inputs = evaluation_inputs()
    front = mutate_contract(
        list(inputs["front_chain"]),  # type: ignore[arg-type]
        OptionRight.PUT,
        "95",
        open_interest=None,
        open_interest_date=None,
    )
    front.extend(
        [
            option("92.5", OptionRight.PUT, "0.80", "0.90", delta="-0.12"),
            option("107.5", OptionRight.CALL, "0.55", "0.60", delta="0.12"),
        ]
    )
    inputs["front_chain"] = front

    candidate = evaluate_symbol(**inputs).candidate  # type: ignore[arg-type]

    assert candidate is not None
    assert candidate.short_put.snapshot.contract.strike == D("92.5")
    assert candidate.short_call.snapshot.contract.strike == D("107.5")
    assert candidate.long_put.snapshot.contract.strike == D("90")
    assert candidate.long_call.snapshot.contract.strike == D("110")
    assert candidate.maximum_loss <= D("500")


def test_mixed_or_unknown_ppind_uses_conservative_non_penny_tick() -> None:
    inputs = evaluation_inputs()
    front = mutate_quote(
        list(inputs["front_chain"]),  # type: ignore[arg-type]
        OptionRight.CALL,
        "105",
        ask=D("1.54"),
    )
    all_penny = evaluate_symbol(**{**inputs, "front_chain": front}).candidate  # type: ignore[arg-type]
    assert all_penny is not None
    assert all_penny.midpoint_credit == D("2.470")
    assert all_penny.proposed_credit == D("2.47")

    front = mutate_contract(front, OptionRight.PUT, "90", ppind=None)
    conservative = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    ).candidate
    assert conservative is not None
    assert conservative.tick_size == D("0.05")
    assert conservative.proposed_credit == D("2.45")


@pytest.mark.parametrize(
    ("underlying", "expected_code"),
    [
        (
            UnderlyingQuote(D("0"), D("100"), OBSERVED_AT),
            GateCode.UNDERLYING_QUOTE_INVALID,
        ),
        (
            UnderlyingQuote(
                D("99.99"), D("100.01"), OBSERVED_AT - timedelta(seconds=11)
            ),
            GateCode.UNDERLYING_QUOTE_STALE,
        ),
        (
            UnderlyingQuote(D("99"), D("101"), OBSERVED_AT),
            GateCode.UNDERLYING_SPREAD_WIDE,
        ),
    ],
)
def test_underlying_hard_gates(
    underlying: UnderlyingQuote, expected_code: GateCode
) -> None:
    result = evaluate_symbol(
        **{**evaluation_inputs(), "underlying": underlying}  # type: ignore[arg-type]
    )
    assert result.candidate is None
    assert expected_code in result.failure_codes


def test_low_iv_ratio_rejects_candidate() -> None:
    inputs = evaluation_inputs()
    back = list(inputs["back_chain"])  # type: ignore[arg-type]
    for right in (OptionRight.PUT, OptionRight.CALL):
        back = mutate_quote(back, right, "100", implied_volatility=D("0.60"))
    result = evaluate_symbol(
        **{**inputs, "back_chain": back}  # type: ignore[arg-type]
    )
    assert result.failure_codes == (GateCode.IV_RATIO_LOW,)


def test_expected_move_outside_range_rejects_candidate() -> None:
    inputs = evaluation_inputs()
    front = list(inputs["front_chain"])  # type: ignore[arg-type]
    for right in (OptionRight.PUT, OptionRight.CALL):
        front = mutate_quote(front, right, "100", bid=D("0.90"), ask=D("1.10"))
    result = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert result.failure_codes == (GateCode.EXPECTED_MOVE_OUT_OF_RANGE,)


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"open_interest": 49}, GateCode.OPEN_INTEREST_LOW),
        (
            {"open_interest_date": date(2026, 8, 28)},
            GateCode.OPEN_INTEREST_MISSING_OR_STALE,
        ),
    ],
)
def test_stale_or_low_short_open_interest_rejects(
    changes: dict[str, object], expected_code: GateCode
) -> None:
    inputs = evaluation_inputs()
    front = mutate_contract(
        list(inputs["front_chain"]),  # type: ignore[arg-type]
        OptionRight.PUT,
        "95",
        **changes,
    )
    result = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert expected_code in result.failure_codes
    assert GateCode.NO_VALID_CONDOR in result.failure_codes


def test_stale_and_wide_option_quote_rejects() -> None:
    inputs = evaluation_inputs()
    front = mutate_quote(
        list(inputs["front_chain"]),  # type: ignore[arg-type]
        OptionRight.CALL,
        "105",
        bid=D("0.20"),
        ask=D("1.30"),
        timestamp=OBSERVED_AT - timedelta(seconds=61),
    )
    result = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert GateCode.OPTION_QUOTE_STALE in result.failure_codes
    assert GateCode.OPTION_SPREAD_WIDE in result.failure_codes


def test_low_credit_rejects_candidate() -> None:
    inputs = evaluation_inputs()
    front = list(inputs["front_chain"])  # type: ignore[arg-type]
    for right, strike in ((OptionRight.PUT, "95"), (OptionRight.CALL, "105")):
        front = mutate_quote(front, right, strike, bid=D("0.25"), ask=D("0.30"))
    result = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert GateCode.CREDIT_TOO_LOW in result.failure_codes
    assert GateCode.NO_VALID_CONDOR in result.failure_codes


def test_risk_budget_and_buying_power_are_independent_gates() -> None:
    inputs = evaluation_inputs()
    low_equity = evaluate_symbol(
        **{**inputs, "initial_equity": D("40000")}  # type: ignore[arg-type]
    )
    assert GateCode.MAXIMUM_LOSS_HIGH in low_equity.failure_codes
    assert GateCode.QUANTITY_ZERO in low_equity.failure_codes

    low_buying_power = evaluate_symbol(
        **{**inputs, "buying_power": D("254.99")}  # type: ignore[arg-type]
    )
    assert GateCode.BUYING_POWER_LOW in low_buying_power.failure_codes


def test_net_delta_gate_applies_only_when_all_leg_deltas_are_valid() -> None:
    inputs = evaluation_inputs()
    front = mutate_quote(
        list(inputs["front_chain"]),  # type: ignore[arg-type]
        OptionRight.PUT,
        "90",
        delta=D("-0.50"),
    )
    high_delta = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert GateCode.NET_DELTA_HIGH in high_delta.failure_codes

    front = mutate_quote(front, OptionRight.CALL, "110", delta=None)
    missing_delta = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert missing_delta.eligible is True
    assert missing_delta.candidate is not None
    assert missing_delta.candidate.net_delta is None


def test_nonstandard_wing_rejects_candidate() -> None:
    inputs = evaluation_inputs()
    front = mutate_contract(
        list(inputs["front_chain"]),  # type: ignore[arg-type]
        OptionRight.PUT,
        "90",
        has_custom_deliverables=True,
    )
    result = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert GateCode.CONTRACT_INELIGIBLE in result.failure_codes


def test_asymmetric_or_too_wide_wings_do_not_form_condor() -> None:
    inputs = evaluation_inputs()
    front = [
        snapshot
        for snapshot in inputs["front_chain"]  # type: ignore[union-attr]
        if not (
            snapshot.contract.right is OptionRight.CALL
            and snapshot.contract.strike == D("110")
        )
    ]
    result = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert result.failure_codes == (
        GateCode.SYMMETRIC_WINGS_MISSING,
        GateCode.NO_VALID_CONDOR,
    )


def test_candidate_ranking_uses_every_tie_break_in_order() -> None:
    base = evaluate_symbol(**evaluation_inputs()).candidate  # type: ignore[arg-type]
    assert base is not None
    candidates = [
        replace(
            base,
            symbol="DDD",
            iv_ratio=D("1.30"),
            aggregate_relative_spread=D("0.20"),
            natural_credit=D("2.0"),
        ),
        replace(
            base,
            symbol="CCC",
            iv_ratio=D("1.20"),
            aggregate_relative_spread=D("0.10"),
            natural_credit=D("2.5"),
        ),
        replace(
            base,
            symbol="BBB",
            iv_ratio=D("1.20"),
            aggregate_relative_spread=D("0.10"),
            natural_credit=D("2.0"),
        ),
        replace(
            base,
            symbol="AAA",
            iv_ratio=D("1.20"),
            aggregate_relative_spread=D("0.10"),
            natural_credit=D("2.0"),
        ),
        replace(
            base,
            symbol="EEE",
            iv_ratio=D("1.20"),
            aggregate_relative_spread=D("0.20"),
            natural_credit=D("4.0"),
        ),
    ]

    assert [candidate.symbol for candidate in rank_candidates(candidates)] == [
        "DDD",  # IV ratio wins before liquidity.
        "CCC",  # Same IV/liquidity; higher natural credit/width.
        "AAA",  # Exact metric tie; alphabetical.
        "BBB",
        "EEE",  # Wider aggregate spread loses before credit/width.
    ]


def test_future_quotes_beyond_clock_skew_are_stale() -> None:
    inputs = evaluation_inputs()
    front = mutate_quote(
        list(inputs["front_chain"]),  # type: ignore[arg-type]
        OptionRight.PUT,
        "95",
        timestamp=OBSERVED_AT + timedelta(seconds=3),
    )
    result = evaluate_symbol(
        **{**inputs, "front_chain": front}  # type: ignore[arg-type]
    )
    assert GateCode.OPTION_QUOTE_STALE in result.failure_codes


def test_strategy_config_can_relax_no_implicit_rule() -> None:
    """Thresholds live in one explicit immutable policy object."""

    config = StrategyConfig(minimum_iv_ratio=D("1.21"))
    result = evaluate_symbol(
        **evaluation_inputs(),
        config=config,  # type: ignore[arg-type]
    )
    assert result.failure_codes == (GateCode.IV_RATIO_LOW,)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"maximum_loss_dollars": D("500.01")}, "maximum_loss_dollars"),
        ({"risk_fraction": D("0.006")}, "risk_fraction"),
        ({"maximum_wing_width": D("5.01")}, "maximum_wing_width"),
        ({"maximum_contracts": 2}, "maximum_contracts"),
        ({"minimum_short_open_interest": 49}, "minimum_short_open_interest"),
    ],
)
def test_strategy_config_cannot_relax_hard_safety_limits(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        StrategyConfig(**change)  # type: ignore[arg-type]
