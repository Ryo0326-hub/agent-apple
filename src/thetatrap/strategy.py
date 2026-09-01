"""Deterministic ThetaTrap candidate construction and risk policy.

This module is deliberately broker- and model-free.  It accepts normalized
snapshots, applies the frozen PRD rules, and either returns one immutable
candidate or finite rejection reasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Iterable, Sequence

from thetatrap.domain import (
    CondorCandidate,
    GateCode,
    GateFailure,
    LegRole,
    OptionRight,
    OptionSnapshot,
    OrderSide,
    StrategyEvaluation,
    StrategyLeg,
    UnderlyingQuote,
)


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
THREE = Decimal("3")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    minimum_iv_ratio: Decimal = Decimal("1.10")
    minimum_expected_move_fraction: Decimal = Decimal("0.05")
    maximum_expected_move_fraction: Decimal = Decimal("0.18")
    maximum_wing_width: Decimal = Decimal("5")
    maximum_underlying_age_seconds: int = 10
    maximum_option_age_seconds: int = 60
    maximum_future_quote_skew_seconds: int = 2
    maximum_underlying_spread_fraction: Decimal = Decimal("0.002")
    maximum_atm_spread_fraction: Decimal = Decimal("0.25")
    maximum_short_spread: Decimal = Decimal("1.00")
    maximum_short_spread_fraction: Decimal = Decimal("0.25")
    maximum_wing_spread: Decimal = Decimal("1.00")
    maximum_wing_spread_fraction: Decimal = Decimal("0.50")
    minimum_short_open_interest: int = 50
    minimum_wing_open_interest: int = 10
    minimum_credit: Decimal = Decimal("0.20")
    minimum_credit_to_width: Decimal = Decimal("0.10")
    maximum_midpoint_natural_gap: Decimal = Decimal("1.00")
    maximum_net_delta: Decimal = Decimal("0.15")
    maximum_loss_dollars: Decimal = Decimal("500")
    risk_fraction: Decimal = Decimal("0.005")
    maximum_contracts: int = 1

    def __post_init__(self) -> None:
        """Allow tighter experiment settings, never weaker hard PRD limits."""

        upper_bounds = {
            "maximum_wing_width": (self.maximum_wing_width, Decimal("5")),
            "maximum_underlying_age_seconds": (
                Decimal(self.maximum_underlying_age_seconds),
                Decimal("10"),
            ),
            "maximum_option_age_seconds": (
                Decimal(self.maximum_option_age_seconds),
                Decimal("60"),
            ),
            "maximum_underlying_spread_fraction": (
                self.maximum_underlying_spread_fraction,
                Decimal("0.002"),
            ),
            "maximum_atm_spread_fraction": (
                self.maximum_atm_spread_fraction,
                Decimal("0.25"),
            ),
            "maximum_short_spread": (self.maximum_short_spread, Decimal("1.00")),
            "maximum_short_spread_fraction": (
                self.maximum_short_spread_fraction,
                Decimal("0.25"),
            ),
            "maximum_wing_spread": (self.maximum_wing_spread, Decimal("1.00")),
            "maximum_wing_spread_fraction": (
                self.maximum_wing_spread_fraction,
                Decimal("0.50"),
            ),
            "maximum_midpoint_natural_gap": (
                self.maximum_midpoint_natural_gap,
                Decimal("1.00"),
            ),
            "maximum_net_delta": (self.maximum_net_delta, Decimal("0.15")),
            "maximum_loss_dollars": (self.maximum_loss_dollars, Decimal("500")),
            "risk_fraction": (self.risk_fraction, Decimal("0.005")),
        }
        for name, (actual, hard_limit) in upper_bounds.items():
            if actual <= ZERO or actual > hard_limit:
                raise ValueError(
                    f"{name} must be positive and no greater than {hard_limit}"
                )
        lower_bounds = {
            "minimum_iv_ratio": (self.minimum_iv_ratio, Decimal("1.10")),
            "minimum_expected_move_fraction": (
                self.minimum_expected_move_fraction,
                Decimal("0.05"),
            ),
            "minimum_short_open_interest": (
                Decimal(self.minimum_short_open_interest),
                Decimal("50"),
            ),
            "minimum_wing_open_interest": (
                Decimal(self.minimum_wing_open_interest),
                Decimal("10"),
            ),
            "minimum_credit": (self.minimum_credit, Decimal("0.20")),
            "minimum_credit_to_width": (
                self.minimum_credit_to_width,
                Decimal("0.10"),
            ),
        }
        for name, (actual, hard_limit) in lower_bounds.items():
            if actual < hard_limit:
                raise ValueError(f"{name} cannot be lower than {hard_limit}")
        if (
            self.maximum_expected_move_fraction > Decimal("0.18")
            or self.maximum_expected_move_fraction < self.minimum_expected_move_fraction
        ):
            raise ValueError(
                "maximum_expected_move_fraction must be between the configured minimum and 0.18"
            )
        if self.maximum_contracts != 1:
            raise ValueError("maximum_contracts must remain exactly 1")


@dataclass(frozen=True, slots=True)
class AtmPair:
    strike: Decimal
    call: OptionSnapshot
    put: OptionSnapshot


def select_atm_pair(
    chain: Sequence[OptionSnapshot], spot: Decimal, expiration: date
) -> AtmPair | None:
    """Select the nearest strike containing both rights; lower strike wins ties."""

    by_key: dict[tuple[Decimal, OptionRight], OptionSnapshot] = {}
    for snapshot in sorted(chain, key=lambda item: item.contract.symbol):
        contract = snapshot.contract
        if contract.expiration != expiration:
            continue
        by_key.setdefault((contract.strike, contract.right), snapshot)

    common = sorted(
        {
            strike
            for strike, right in by_key
            if right is OptionRight.CALL and (strike, OptionRight.PUT) in by_key
        },
        key=lambda strike: (abs(strike - spot), strike),
    )
    if not common:
        return None
    strike = common[0]
    return AtmPair(
        strike=strike,
        call=by_key[(strike, OptionRight.CALL)],
        put=by_key[(strike, OptionRight.PUT)],
    )


def option_tick_size(price: Decimal, ppind: bool | None) -> Decimal:
    """Return the standard equity-option tick for a positive net price.

    ``ppind`` is Alpaca's Penny Program Indicator.  Unknown eligibility is
    handled conservatively as non-penny.  Penny contracts use $0.01 below $3
    and $0.05 at/above $3; other contracts use $0.05 and $0.10 respectively.
    """

    if price < ZERO:
        raise ValueError("option price cannot be negative")
    if ppind is True:
        return Decimal("0.01") if price < THREE else Decimal("0.05")
    return Decimal("0.05") if price < THREE else Decimal("0.10")


def round_credit_down(price: Decimal, ppind: bool | None) -> tuple[Decimal, Decimal]:
    tick = option_tick_size(price, ppind)
    rounded = (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return rounded.quantize(tick), tick


def round_debit_up(price: Decimal, ppind: bool | None) -> tuple[Decimal, Decimal]:
    tick = option_tick_size(price, ppind)
    rounded = (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    return rounded.quantize(tick), tick


def evaluate_symbol(
    *,
    symbol: str,
    observed_at: datetime,
    underlying: UnderlyingQuote,
    front_chain: Sequence[OptionSnapshot],
    back_chain: Sequence[OptionSnapshot],
    trade_expiration: date,
    term_expiration: date,
    previous_trading_day: date,
    initial_equity: Decimal,
    buying_power: Decimal,
    config: StrategyConfig | None = None,
) -> StrategyEvaluation:
    """Build the narrowest eligible symmetric earnings iron condor."""

    policy = config or StrategyConfig()
    _require_aware(observed_at)
    as_of = observed_at.astimezone(UTC)
    normalized_symbol = symbol.strip().upper()

    underlying_failures = _validate_underlying(underlying, as_of, policy)
    if underlying_failures:
        return _rejected(normalized_symbol, underlying_failures)
    spot = underlying.midpoint

    front = _for_symbol(front_chain, normalized_symbol)
    back = _for_symbol(back_chain, normalized_symbol)
    front_atm = select_atm_pair(front, spot, trade_expiration)
    if front_atm is None:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.FRONT_ATM_PAIR_MISSING, "no front ATM call/put pair"
                )
            ],
        )
    back_atm = select_atm_pair(back, spot, term_expiration)
    if back_atm is None:
        return _rejected(
            normalized_symbol,
            [GateFailure(GateCode.BACK_ATM_PAIR_MISSING, "no back ATM call/put pair")],
        )

    atm_failures = _validate_atm_pair(front_atm, as_of, policy, "front")
    atm_failures.extend(_validate_atm_pair(back_atm, as_of, policy, "back"))
    if atm_failures:
        return _rejected(normalized_symbol, atm_failures)

    front_iv = _atm_iv(front_atm)
    back_iv = _atm_iv(back_atm)
    if front_iv is None or back_iv is None or back_iv <= ZERO:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.ATM_IV_MISSING,
                    "ATM IV must be positive for both expirations",
                )
            ],
        )
    iv_ratio = front_iv / back_iv
    if iv_ratio < policy.minimum_iv_ratio:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.IV_RATIO_LOW,
                    f"front/back ATM IV ratio {iv_ratio} is below {policy.minimum_iv_ratio}",
                )
            ],
        )

    expected_move = front_atm.call.quote.midpoint + front_atm.put.quote.midpoint
    expected_move_fraction = expected_move / spot
    if not (
        policy.minimum_expected_move_fraction
        <= expected_move_fraction
        <= policy.maximum_expected_move_fraction
    ):
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.EXPECTED_MOVE_OUT_OF_RANGE,
                    f"expected move fraction {expected_move_fraction} is outside policy",
                )
            ],
        )

    trade_chain = [
        snapshot
        for snapshot in front
        if snapshot.contract.expiration == trade_expiration
    ]
    put_threshold = spot - expected_move
    call_threshold = spot + expected_move
    short_puts = _short_put_candidates(trade_chain, put_threshold)
    short_calls = _short_call_candidates(trade_chain, call_threshold)
    if not short_puts or not short_calls:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.SHORT_STRIKE_MISSING,
                    "no put and call strikes exist outside the expected move",
                )
            ],
        )

    attempted_failures: list[GateFailure] = []
    attempted_structures = 0
    for short_put, short_call in _ordered_short_pairs(
        short_puts,
        short_calls,
        put_threshold=put_threshold,
        call_threshold=call_threshold,
    ):
        short_failures = _validate_trade_leg(
            short_put, as_of, previous_trading_day, short=True, config=policy
        )
        short_failures.extend(
            _validate_trade_leg(
                short_call, as_of, previous_trading_day, short=True, config=policy
            )
        )
        if short_failures:
            attempted_failures.extend(short_failures)
            continue

        wing_sets = _symmetric_wing_sets(
            trade_chain, short_put, short_call, policy.maximum_wing_width
        )
        if not wing_sets:
            attempted_failures.append(
                GateFailure(
                    GateCode.SYMMETRIC_WINGS_MISSING,
                    "no equal-width protective wings at or below maximum width",
                )
            )
            continue

        for wing_width, long_put, long_call in wing_sets:
            attempted_structures += 1
            candidate, failures = _evaluate_structure(
                symbol=normalized_symbol,
                observed_at=as_of,
                spot=spot,
                front_atm=front_atm,
                back_atm=back_atm,
                expected_move=expected_move,
                expected_move_fraction=expected_move_fraction,
                front_iv=front_iv,
                back_iv=back_iv,
                iv_ratio=iv_ratio,
                short_put=short_put,
                short_call=short_call,
                long_put=long_put,
                long_call=long_call,
                wing_width=wing_width,
                previous_trading_day=previous_trading_day,
                initial_equity=initial_equity,
                buying_power=buying_power,
                config=policy,
            )
            if candidate is not None:
                return StrategyEvaluation(normalized_symbol, candidate)
            attempted_failures.extend(failures)

    attempted_failures.append(
        GateFailure(
            GateCode.NO_VALID_CONDOR,
            "no liquid short pair and symmetric wing set passed "
            f"across {attempted_structures} priced structures",
        )
    )
    return _rejected(normalized_symbol, attempted_failures)


def rank_candidates(
    candidates: Iterable[CondorCandidate],
) -> tuple[CondorCandidate, ...]:
    """Apply the frozen rank order with an alphabetical final tie-break."""

    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.iv_ratio,
                candidate.aggregate_relative_spread,
                -candidate.credit_to_width,
                candidate.symbol,
            ),
        )
    )


def _evaluate_structure(
    *,
    symbol: str,
    observed_at: datetime,
    spot: Decimal,
    front_atm: AtmPair,
    back_atm: AtmPair,
    expected_move: Decimal,
    expected_move_fraction: Decimal,
    front_iv: Decimal,
    back_iv: Decimal,
    iv_ratio: Decimal,
    short_put: OptionSnapshot,
    short_call: OptionSnapshot,
    long_put: OptionSnapshot,
    long_call: OptionSnapshot,
    wing_width: Decimal,
    previous_trading_day: date,
    initial_equity: Decimal,
    buying_power: Decimal,
    config: StrategyConfig,
) -> tuple[CondorCandidate | None, list[GateFailure]]:
    failures = _validate_trade_leg(
        long_put, observed_at, previous_trading_day, short=False, config=config
    )
    failures.extend(
        _validate_trade_leg(
            long_call, observed_at, previous_trading_day, short=False, config=config
        )
    )
    if failures:
        return None, failures

    snapshots = (long_put, short_put, short_call, long_call)
    natural_credit = (
        short_put.quote.bid
        + short_call.quote.bid
        - long_put.quote.ask
        - long_call.quote.ask
    )
    midpoint_credit = (
        short_put.quote.midpoint
        + short_call.quote.midpoint
        - long_put.quote.midpoint
        - long_call.quote.midpoint
    )
    if midpoint_credit < ZERO:
        failures.append(
            GateFailure(GateCode.CREDIT_TOO_LOW, "midpoint credit is negative")
        )
        return None, failures

    penny_combination = all(snapshot.contract.ppind is True for snapshot in snapshots)
    proposed_credit, tick_size = round_credit_down(midpoint_credit, penny_combination)
    minimum_credit = max(
        config.minimum_credit, config.minimum_credit_to_width * wing_width
    )
    if natural_credit < minimum_credit or proposed_credit < minimum_credit:
        failures.append(
            GateFailure(
                GateCode.CREDIT_TOO_LOW,
                f"natural/proposed credit must each be at least {minimum_credit}",
            )
        )
    if proposed_credit >= wing_width:
        failures.append(
            GateFailure(
                GateCode.CREDIT_NOT_DEFINED_RISK,
                "submitted credit must remain below wing width",
            )
        )
    if midpoint_credit - natural_credit > config.maximum_midpoint_natural_gap:
        failures.append(
            GateFailure(
                GateCode.MIDPOINT_NATURAL_GAP_WIDE,
                "midpoint-to-natural credit gap exceeds policy",
            )
        )

    legs = (
        StrategyLeg(LegRole.LONG_PUT, OrderSide.BUY, long_put),
        StrategyLeg(LegRole.SHORT_PUT, OrderSide.SELL, short_put),
        StrategyLeg(LegRole.SHORT_CALL, OrderSide.SELL, short_call),
        StrategyLeg(LegRole.LONG_CALL, OrderSide.BUY, long_call),
    )
    net_delta = _net_position_delta(legs)
    if net_delta is not None and abs(net_delta) > config.maximum_net_delta:
        failures.append(
            GateFailure(
                GateCode.NET_DELTA_HIGH,
                f"absolute net delta {abs(net_delta)} exceeds {config.maximum_net_delta}",
            )
        )

    maximum_loss = (wing_width - proposed_credit) * HUNDRED
    risk_budget = min(
        config.maximum_loss_dollars,
        max(ZERO, initial_equity * config.risk_fraction),
    )
    if maximum_loss <= ZERO or maximum_loss > risk_budget:
        failures.append(
            GateFailure(
                GateCode.MAXIMUM_LOSS_HIGH,
                f"maximum loss {maximum_loss} exceeds risk budget {risk_budget}",
            )
        )
    quantity = (
        min(config.maximum_contracts, int(risk_budget // maximum_loss))
        if maximum_loss > ZERO
        else 0
    )
    if quantity < 1:
        failures.append(
            GateFailure(GateCode.QUANTITY_ZERO, "risk-sized quantity is zero")
        )
    if buying_power < maximum_loss:
        failures.append(
            GateFailure(
                GateCode.BUYING_POWER_LOW,
                f"buying power {buying_power} is below maximum loss {maximum_loss}",
            )
        )
    if failures:
        return None, failures

    spread_sum = sum((snapshot.quote.spread for snapshot in snapshots), ZERO)
    midpoint_sum = sum((snapshot.quote.midpoint for snapshot in snapshots), ZERO)
    aggregate_relative_spread = spread_sum / midpoint_sum

    return (
        CondorCandidate(
            symbol=symbol,
            observed_at=observed_at,
            spot=spot,
            front_atm_strike=front_atm.strike,
            back_atm_strike=back_atm.strike,
            expected_move=expected_move,
            expected_move_fraction=expected_move_fraction,
            front_atm_iv=front_iv,
            back_atm_iv=back_iv,
            iv_ratio=iv_ratio,
            long_put=legs[0],
            short_put=legs[1],
            short_call=legs[2],
            long_call=legs[3],
            wing_width=wing_width,
            natural_credit=natural_credit,
            midpoint_credit=midpoint_credit,
            proposed_credit=proposed_credit,
            tick_size=tick_size,
            maximum_profit=proposed_credit * HUNDRED,
            maximum_loss=maximum_loss,
            risk_budget=risk_budget,
            quantity=quantity,
            aggregate_relative_spread=aggregate_relative_spread,
            net_delta=net_delta,
        ),
        [],
    )


def _validate_underlying(
    quote: UnderlyingQuote, observed_at: datetime, config: StrategyConfig
) -> list[GateFailure]:
    failures: list[GateFailure] = []
    if quote.bid <= ZERO or quote.ask <= quote.bid:
        failures.append(
            GateFailure(
                GateCode.UNDERLYING_QUOTE_INVALID,
                "underlying bid must be positive and ask must exceed bid",
            )
        )
        return failures
    if not _is_fresh(
        quote.timestamp,
        observed_at,
        config.maximum_underlying_age_seconds,
        config.maximum_future_quote_skew_seconds,
    ):
        failures.append(
            GateFailure(GateCode.UNDERLYING_QUOTE_STALE, "underlying quote is stale")
        )
    if quote.spread / quote.midpoint > config.maximum_underlying_spread_fraction:
        failures.append(
            GateFailure(
                GateCode.UNDERLYING_SPREAD_WIDE,
                "underlying relative spread exceeds policy",
            )
        )
    return failures


def _validate_atm_pair(
    pair: AtmPair,
    observed_at: datetime,
    config: StrategyConfig,
    label: str,
) -> list[GateFailure]:
    failures: list[GateFailure] = []
    for snapshot in (pair.call, pair.put):
        contract = snapshot.contract
        quote = snapshot.quote
        if (
            not contract.tradable
            or contract.status != "active"
            or not contract.is_standard
        ):
            failures.append(
                GateFailure(
                    GateCode.CONTRACT_INELIGIBLE,
                    f"{label} ATM contract {contract.symbol} is not active/standard/tradable",
                )
            )
        if quote.bid <= ZERO or quote.ask <= quote.bid:
            failures.append(
                GateFailure(
                    GateCode.ATM_QUOTE_INVALID,
                    f"{label} ATM quote {contract.symbol} is invalid",
                )
            )
            continue
        if not _is_fresh(
            quote.timestamp,
            observed_at,
            config.maximum_option_age_seconds,
            config.maximum_future_quote_skew_seconds,
        ):
            failures.append(
                GateFailure(
                    GateCode.ATM_QUOTE_STALE,
                    f"{label} ATM quote {contract.symbol} is stale",
                )
            )
        relative_spread = quote.relative_spread
        if (
            relative_spread is None
            or relative_spread > config.maximum_atm_spread_fraction
        ):
            failures.append(
                GateFailure(
                    GateCode.ATM_SPREAD_WIDE,
                    f"{label} ATM spread {contract.symbol} exceeds policy",
                )
            )
        if quote.implied_volatility is None or quote.implied_volatility <= ZERO:
            failures.append(
                GateFailure(
                    GateCode.ATM_IV_MISSING,
                    f"{label} ATM IV {contract.symbol} is missing or non-positive",
                )
            )
    return failures


def _validate_trade_leg(
    snapshot: OptionSnapshot,
    observed_at: datetime,
    previous_trading_day: date,
    *,
    short: bool,
    config: StrategyConfig,
) -> list[GateFailure]:
    contract = snapshot.contract
    quote = snapshot.quote
    failures: list[GateFailure] = []
    label = "short" if short else "wing"
    if not contract.tradable or contract.status != "active" or not contract.is_standard:
        failures.append(
            GateFailure(
                GateCode.CONTRACT_INELIGIBLE,
                f"{label} contract {contract.symbol} is not active/standard/tradable",
            )
        )
    if (
        contract.open_interest is None
        or contract.open_interest_date is None
        or contract.open_interest_date < previous_trading_day
    ):
        failures.append(
            GateFailure(
                GateCode.OPEN_INTEREST_MISSING_OR_STALE,
                f"{label} contract {contract.symbol} lacks current open interest",
            )
        )
    else:
        minimum_oi = (
            config.minimum_short_open_interest
            if short
            else config.minimum_wing_open_interest
        )
        if contract.open_interest < minimum_oi:
            failures.append(
                GateFailure(
                    GateCode.OPEN_INTEREST_LOW,
                    f"{label} contract {contract.symbol} open interest is below {minimum_oi}",
                )
            )
    if quote.bid <= ZERO or quote.ask <= quote.bid:
        failures.append(
            GateFailure(
                GateCode.OPTION_QUOTE_INVALID,
                f"{label} quote {contract.symbol} must have bid>0 and ask>bid",
            )
        )
        return failures
    if not _is_fresh(
        quote.timestamp,
        observed_at,
        config.maximum_option_age_seconds,
        config.maximum_future_quote_skew_seconds,
    ):
        failures.append(
            GateFailure(
                GateCode.OPTION_QUOTE_STALE,
                f"{label} quote {contract.symbol} is stale",
            )
        )
    relative_spread = quote.relative_spread
    absolute_limit = (
        config.maximum_short_spread if short else config.maximum_wing_spread
    )
    relative_limit = (
        config.maximum_short_spread_fraction
        if short
        else config.maximum_wing_spread_fraction
    )
    if (
        quote.spread > absolute_limit
        or relative_spread is None
        or relative_spread > relative_limit
    ):
        failures.append(
            GateFailure(
                GateCode.OPTION_SPREAD_WIDE,
                f"{label} spread {contract.symbol} exceeds policy",
            )
        )
    return failures


def _short_put_candidates(
    chain: Sequence[OptionSnapshot], threshold: Decimal
) -> list[OptionSnapshot]:
    return sorted(
        [
            snapshot
            for snapshot in chain
            if snapshot.contract.right is OptionRight.PUT
            and snapshot.contract.strike <= threshold
        ],
        key=lambda snapshot: (-snapshot.contract.strike, snapshot.contract.symbol),
    )


def _short_call_candidates(
    chain: Sequence[OptionSnapshot], threshold: Decimal
) -> list[OptionSnapshot]:
    return sorted(
        [
            snapshot
            for snapshot in chain
            if snapshot.contract.right is OptionRight.CALL
            and snapshot.contract.strike >= threshold
        ],
        key=lambda snapshot: (snapshot.contract.strike, snapshot.contract.symbol),
    )


def _ordered_short_pairs(
    puts: Sequence[OptionSnapshot],
    calls: Sequence[OptionSnapshot],
    *,
    put_threshold: Decimal,
    call_threshold: Decimal,
) -> list[tuple[OptionSnapshot, OptionSnapshot]]:
    """Search nearest short strikes first while permitting Basic-data fallback.

    Alpaca Basic can omit current open interest or publish an unusable spread for
    one strike while adjacent listed contracts remain valid.  The deterministic
    search therefore advances outward instead of treating the first listed
    strike as the only possible structure.  Hard leg, credit, and risk gates are
    unchanged and every candidate remains outside the expected move.
    """

    pairs = [(put, call) for put in puts for call in calls]
    return sorted(
        pairs,
        key=lambda pair: (
            max(
                put_threshold - pair[0].contract.strike,
                pair[1].contract.strike - call_threshold,
            ),
            abs(
                (put_threshold - pair[0].contract.strike)
                - (pair[1].contract.strike - call_threshold)
            ),
            (put_threshold - pair[0].contract.strike)
            + (pair[1].contract.strike - call_threshold),
            -pair[0].contract.strike,
            pair[1].contract.strike,
            pair[0].contract.symbol,
            pair[1].contract.symbol,
        ),
    )


def _symmetric_wing_sets(
    chain: Sequence[OptionSnapshot],
    short_put: OptionSnapshot,
    short_call: OptionSnapshot,
    maximum_width: Decimal,
) -> list[tuple[Decimal, OptionSnapshot, OptionSnapshot]]:
    puts: dict[Decimal, OptionSnapshot] = {}
    calls: dict[Decimal, OptionSnapshot] = {}
    for snapshot in sorted(chain, key=lambda item: item.contract.symbol):
        contract = snapshot.contract
        if (
            contract.right is OptionRight.PUT
            and contract.strike < short_put.contract.strike
        ):
            width = short_put.contract.strike - contract.strike
            if width <= maximum_width:
                puts.setdefault(width, snapshot)
        elif (
            contract.right is OptionRight.CALL
            and contract.strike > short_call.contract.strike
        ):
            width = contract.strike - short_call.contract.strike
            if width <= maximum_width:
                calls.setdefault(width, snapshot)
    return [
        (width, puts[width], calls[width])
        for width in sorted(puts.keys() & calls.keys())
    ]


def _atm_iv(pair: AtmPair) -> Decimal | None:
    call_iv = pair.call.quote.implied_volatility
    put_iv = pair.put.quote.implied_volatility
    if call_iv is None or put_iv is None:
        return None
    return (call_iv + put_iv) / Decimal("2")


def _net_position_delta(legs: Sequence[StrategyLeg]) -> Decimal | None:
    deltas = [leg.snapshot.quote.delta for leg in legs]
    if any(delta is None or not (-ONE <= delta <= ONE) for delta in deltas):
        return None
    return sum(
        (
            leg.position_sign * leg.snapshot.quote.delta  # type: ignore[operator]
            for leg in legs
        ),
        ZERO,
    )


def _is_fresh(
    timestamp: datetime,
    observed_at: datetime,
    maximum_age_seconds: int,
    maximum_future_skew_seconds: int,
) -> bool:
    quote_time = timestamp.astimezone(UTC)
    age = observed_at - quote_time
    return (
        -timedelta(seconds=maximum_future_skew_seconds)
        <= age
        <= timedelta(seconds=maximum_age_seconds)
    )


def _for_symbol(chain: Sequence[OptionSnapshot], symbol: str) -> list[OptionSnapshot]:
    return [
        snapshot
        for snapshot in chain
        if snapshot.contract.underlying_symbol.strip().upper() == symbol
    ]


def _rejected(symbol: str, failures: Sequence[GateFailure]) -> StrategyEvaluation:
    unique: list[GateFailure] = []
    seen: set[GateCode] = set()
    for failure in failures:
        if failure.code not in seen:
            unique.append(failure)
            seen.add(failure.code)
    return StrategyEvaluation(symbol=symbol, candidate=None, failures=tuple(unique))


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
