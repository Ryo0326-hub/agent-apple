from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from thetatrap.domain import (
    GateCode,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionSnapshot,
    UnderlyingQuote,
)
from thetatrap.intraday import (
    EXPIRATION,
    INTRADAY_PROFILE_ID,
    INTRADAY_STRATEGY_NAME,
    INTRADAY_STRATEGY_VERSION,
    IntradayStrategyConfig,
    evaluate_intraday_symbol,
    rank_intraday_candidates,
)
from thetatrap.orders import build_entry_order_intent


D = Decimal
OBSERVED_AT = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
OI_DATE = date(2026, 8, 31)


def option(
    strike: str,
    right: OptionRight,
    bid: str,
    ask: str,
    *,
    symbol: str = "QQQ",
    expiration: date = EXPIRATION,
    open_interest: int | None = 1_000,
    open_interest_date: date | None = OI_DATE,
    tradable: bool = True,
    status: str = "active",
    multiplier: str = "100",
    size: str = "100",
    ppind: bool | None = True,
    timestamp: datetime = OBSERVED_AT - timedelta(seconds=5),
    delta: str | None = None,
) -> OptionSnapshot:
    strike_decimal = D(strike)
    right_letter = "C" if right is OptionRight.CALL else "P"
    occ_strike = int(strike_decimal * 1_000)
    return OptionSnapshot(
        contract=OptionContract(
            symbol=f"{symbol}260904{right_letter}{occ_strike:08d}",
            underlying_symbol=symbol,
            expiration=expiration,
            right=right,
            strike=strike_decimal,
            tradable=tradable,
            status=status,
            multiplier=D(multiplier),
            size=D(size),
            open_interest=open_interest,
            open_interest_date=open_interest_date,
            ppind=ppind,
        ),
        quote=OptionQuote(
            bid=D(bid),
            ask=D(ask),
            timestamp=timestamp,
            implied_volatility=None,
            delta=D(delta) if delta is not None else None,
        ),
    )


def valid_chain(*, symbol: str = "QQQ") -> list[OptionSnapshot]:
    return [
        option("498", OptionRight.PUT, "0.35", "0.40", symbol=symbol),
        option("499", OptionRight.PUT, "0.60", "0.65", symbol=symbol),
        option("501", OptionRight.CALL, "0.65", "0.70", symbol=symbol),
        option("502", OptionRight.CALL, "0.35", "0.40", symbol=symbol),
    ]


def evaluation_inputs(*, symbol: str = "QQQ") -> dict[str, object]:
    return {
        "symbol": symbol,
        "observed_at": OBSERVED_AT,
        "underlying": UnderlyingQuote(
            bid=D("499.99"),
            ask=D("500.01"),
            timestamp=OBSERVED_AT - timedelta(seconds=2),
        ),
        "option_chain": valid_chain(symbol=symbol),
        "buying_power": D("400000"),
    }


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


def test_profile_yaml_is_truthful_and_frozen_to_final_day_contingency() -> None:
    profile = yaml.safe_load(Path("config/intraday.yaml").read_text(encoding="utf-8"))

    assert profile["profile_kind"] == "final_day_intraday_contingency"
    assert profile["profile_id"] == INTRADAY_PROFILE_ID
    assert profile["strategy_name"] == INTRADAY_STRATEGY_NAME
    assert profile["strategy_version"] == INTRADAY_STRATEGY_VERSION
    assert profile["trade_date"] == "2026-09-03"
    assert profile["expiration"] == "2026-09-04"
    assert profile["universe"] == ["QQQ", "SPY"]
    assert profile["entry_window"] == {
        "start": "09:45",
        "stop_new_orders": "10:45",
        "cancel_all_unfilled": "10:50",
    }
    assert profile["exit_window"] == {
        "start": "15:15",
        "aggressive_limit": "15:25",
        "broker_flat_deadline": "15:45",
    }
    assert profile["structure"] == {
        "name": "one_dollar_symmetric_iron_condor",
        "quantity": 1,
        "wing_width": "1.00",
        "minimum_proposed_credit": "0.20",
        "maximum_loss_dollars": "80.00",
    }
    assert profile["market_data_policy"]["accepted_open_interest_dates"] == [
        "2026-09-02",
        "2026-09-01",
        "2026-08-31",
    ]
    assert "not established a profitable edge" in profile["disclosures"][-2]


def test_valid_t3_condor_returns_order_compatible_candidate_without_iv() -> None:
    result = evaluate_intraday_symbol(**evaluation_inputs())

    assert result.eligible is True
    candidate = result.candidate
    assert candidate is not None
    assert candidate.symbol == "QQQ"
    assert candidate.quantity == 1
    assert candidate.wing_width == D("1.00")
    assert candidate.natural_credit == D("0.45")
    assert candidate.midpoint_credit == D("0.55")
    assert candidate.proposed_credit == D("0.55")
    assert candidate.maximum_loss == D("45.00")
    assert candidate.maximum_loss <= D("80")
    assert candidate.expected_move == 0
    assert candidate.front_atm_iv == 0
    assert candidate.back_atm_iv == 0
    assert candidate.net_delta is None

    intent = build_entry_order_intent(
        candidate,
        environment="competition",
        account_id="private-account-id",
        event_date=date(2026, 9, 3),
        strategy_version="2.0-sep3-canary",
    )
    assert intent.arguments["qty"] == "1"
    assert intent.arguments["limit_price"] == "-0.55"
    assert [leg["side"] for leg in intent.arguments["legs"]] == [
        "buy",
        "sell",
        "sell",
        "buy",
    ]


@pytest.mark.parametrize("symbol", ["QQQ", "SPY", " qqq "])
def test_only_frozen_etf_universe_is_eligible(symbol: str) -> None:
    normalized = symbol.strip().upper()
    result = evaluate_intraday_symbol(**evaluation_inputs(symbol=normalized))

    assert result.eligible is True


def test_unsupported_symbol_is_rejected() -> None:
    inputs = evaluation_inputs()
    inputs["symbol"] = "IWM"

    result = evaluate_intraday_symbol(**inputs)

    assert result.failure_codes == (GateCode.CONTRACT_INELIGIBLE,)


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (
            UnderlyingQuote(
                bid=D("0"), ask=D("500"), timestamp=OBSERVED_AT
            ),
            GateCode.UNDERLYING_QUOTE_INVALID,
        ),
        (
            UnderlyingQuote(
                bid=D("499.99"),
                ask=D("500.01"),
                timestamp=OBSERVED_AT - timedelta(seconds=11),
            ),
            GateCode.UNDERLYING_QUOTE_STALE,
        ),
        (
            UnderlyingQuote(
                bid=D("499"), ask=D("501.01"), timestamp=OBSERVED_AT
            ),
            GateCode.UNDERLYING_SPREAD_WIDE,
        ),
    ],
)
def test_underlying_gates_reject(quote: UnderlyingQuote, expected: GateCode) -> None:
    inputs = evaluation_inputs()
    inputs["underlying"] = quote

    result = evaluate_intraday_symbol(**inputs)

    assert expected in result.failure_codes


def test_naive_observation_time_is_rejected() -> None:
    inputs = evaluation_inputs()
    inputs["observed_at"] = OBSERVED_AT.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone"):
        evaluate_intraday_symbol(**inputs)


def test_wrong_expiration_has_no_short_pair() -> None:
    inputs = evaluation_inputs()
    inputs["option_chain"] = [
        replace(
            snapshot,
            contract=replace(snapshot.contract, expiration=date(2026, 9, 11)),
        )
        for snapshot in valid_chain()
    ]

    result = evaluate_intraday_symbol(**inputs)

    assert result.failure_codes == (GateCode.SHORT_STRIKE_MISSING,)


def test_missing_exact_one_dollar_wing_rejects_geometry() -> None:
    inputs = evaluation_inputs()
    inputs["option_chain"] = valid_chain()[:-1]

    result = evaluate_intraday_symbol(**inputs)

    assert result.failure_codes == (
        GateCode.SYMMETRIC_WINGS_MISSING,
        GateCode.NO_VALID_CONDOR,
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"tradable": False}, GateCode.CONTRACT_INELIGIBLE),
        ({"status": "inactive"}, GateCode.CONTRACT_INELIGIBLE),
        ({"size": D("10")}, GateCode.CONTRACT_INELIGIBLE),
        ({"open_interest": None}, GateCode.OPEN_INTEREST_MISSING_OR_STALE),
        (
            {"open_interest_date": date(2026, 8, 28)},
            GateCode.OPEN_INTEREST_MISSING_OR_STALE,
        ),
        ({"open_interest": 499}, GateCode.OPEN_INTEREST_LOW),
    ],
)
def test_short_contract_and_open_interest_gates_reject(
    changes: dict[str, object], expected: GateCode
) -> None:
    inputs = evaluation_inputs()
    inputs["option_chain"] = mutate_contract(
        valid_chain(), OptionRight.PUT, "499", **changes
    )

    result = evaluate_intraday_symbol(**inputs)

    assert expected in result.failure_codes
    assert GateCode.NO_VALID_CONDOR in result.failure_codes


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"open_interest": None}, GateCode.OPEN_INTEREST_MISSING_OR_STALE),
        ({"open_interest": 99}, GateCode.OPEN_INTEREST_LOW),
    ],
)
def test_wing_open_interest_gates_reject(
    changes: dict[str, object], expected: GateCode
) -> None:
    inputs = evaluation_inputs()
    inputs["option_chain"] = mutate_contract(
        valid_chain(), OptionRight.PUT, "498", **changes
    )

    result = evaluate_intraday_symbol(**inputs)

    assert expected in result.failure_codes


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"bid": D("0")}, GateCode.OPTION_QUOTE_INVALID),
        (
            {"timestamp": OBSERVED_AT - timedelta(seconds=61)},
            GateCode.OPTION_QUOTE_STALE,
        ),
        ({"bid": D("0.10"), "ask": D("0.20")}, GateCode.OPTION_SPREAD_WIDE),
    ],
)
def test_option_quote_gates_reject(
    changes: dict[str, object], expected: GateCode
) -> None:
    inputs = evaluation_inputs()
    inputs["option_chain"] = mutate_quote(
        valid_chain(), OptionRight.PUT, "499", **changes
    )

    result = evaluate_intraday_symbol(**inputs)

    assert expected in result.failure_codes
    assert GateCode.NO_VALID_CONDOR in result.failure_codes


def test_low_credit_rejects_structure() -> None:
    chain = valid_chain()
    chain = mutate_quote(
        chain, OptionRight.PUT, "499", bid=D("0.43"), ask=D("0.48")
    )
    chain = mutate_quote(
        chain, OptionRight.CALL, "501", bid=D("0.43"), ask=D("0.48")
    )
    inputs = evaluation_inputs()
    inputs["option_chain"] = chain

    result = evaluate_intraday_symbol(**inputs)

    assert GateCode.CREDIT_TOO_LOW in result.failure_codes


def test_tighter_midpoint_gap_and_risk_budget_gates_reject() -> None:
    inputs = evaluation_inputs()
    gap_result = evaluate_intraday_symbol(
        **inputs,
        config=IntradayStrategyConfig(maximum_midpoint_natural_gap=D("0.05")),
    )
    loss_result = evaluate_intraday_symbol(
        **inputs,
        config=IntradayStrategyConfig(maximum_loss_dollars=D("40")),
    )

    assert GateCode.MIDPOINT_NATURAL_GAP_WIDE in gap_result.failure_codes
    assert GateCode.MAXIMUM_LOSS_HIGH in loss_result.failure_codes


def test_low_buying_power_rejects_structure() -> None:
    inputs = evaluation_inputs()
    inputs["buying_power"] = D("44.99")

    result = evaluate_intraday_symbol(**inputs)

    assert GateCode.BUYING_POWER_LOW in result.failure_codes


@pytest.mark.parametrize("open_interest_date", [date(2026, 9, 2), date(2026, 9, 1), OI_DATE])
def test_each_prior_t1_through_t3_open_interest_date_is_accepted(
    open_interest_date: date,
) -> None:
    inputs = evaluation_inputs()
    inputs["option_chain"] = [
        replace(
            snapshot,
            contract=replace(
                snapshot.contract, open_interest_date=open_interest_date
            ),
        )
        for snapshot in valid_chain()
    ]

    assert evaluate_intraday_symbol(**inputs).eligible is True


def test_trade_date_open_interest_is_not_in_frozen_prior_session_set() -> None:
    inputs = evaluation_inputs()
    inputs["option_chain"] = [
        replace(
            snapshot,
            contract=replace(
                snapshot.contract, open_interest_date=date(2026, 9, 3)
            ),
        )
        for snapshot in valid_chain()
    ]

    result = evaluate_intraday_symbol(**inputs)

    assert GateCode.OPEN_INTEREST_MISSING_OR_STALE in result.failure_codes


def test_input_order_does_not_change_selected_candidate() -> None:
    inputs = evaluation_inputs()
    forward = evaluate_intraday_symbol(**inputs)
    inputs["option_chain"] = list(reversed(valid_chain()))
    reversed_result = evaluate_intraday_symbol(**inputs)

    assert forward.candidate == reversed_result.candidate


def test_nearest_structure_wins_final_tie_break() -> None:
    chain = valid_chain()
    chain.extend(
        [
            option("496", OptionRight.PUT, "0.35", "0.40"),
            option("497", OptionRight.PUT, "0.60", "0.65"),
            option("503", OptionRight.CALL, "0.65", "0.70"),
            option("504", OptionRight.CALL, "0.35", "0.40"),
        ]
    )
    inputs = evaluation_inputs()
    inputs["option_chain"] = chain

    result = evaluate_intraday_symbol(**inputs)

    assert result.candidate is not None
    assert result.candidate.short_put.snapshot.contract.strike == D("499")
    assert result.candidate.short_call.snapshot.contract.strike == D("501")


def test_rank_prefers_tighter_aggregate_spread_then_higher_natural_credit() -> None:
    base = evaluate_intraday_symbol(**evaluation_inputs()).candidate
    assert base is not None
    tighter = replace(
        base,
        symbol="SPY",
        aggregate_relative_spread=base.aggregate_relative_spread - D("0.01"),
        natural_credit=base.natural_credit - D("0.10"),
    )
    richer = replace(base, natural_credit=base.natural_credit + D("0.10"))

    ranked = rank_intraday_candidates([richer, tighter, base])

    assert ranked[0] is tighter
    assert ranked[1] is richer
    assert ranked[2] is base


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"allowed_symbols": ("QQQ", "IWM")}, "QQQ and SPY"),
        ({"wing_width": D("2")}, "exact \\$1 wings"),
        ({"quantity": 2}, "one contract"),
        ({"maximum_underlying_age_seconds": 11}, "no greater than 10"),
        ({"maximum_option_age_seconds": 61}, "no greater than 60"),
        ({"maximum_loss_dollars": D("80.01")}, "no greater than 80.00"),
        ({"minimum_short_open_interest": 499}, "lower than 500"),
        ({"minimum_wing_open_interest": 99}, "lower than 100"),
        ({"minimum_proposed_credit": D("0.19")}, "lower than 0.20"),
        (
            {"accepted_open_interest_dates": frozenset({date(2026, 8, 28)})},
            "T-1 through T-3",
        ),
    ],
)
def test_policy_cannot_be_loosened(
    kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        IntradayStrategyConfig(**kwargs)
