"""Pure Sep 3 Intraday Theta Canary candidate construction.

This module is deliberately broker-, model-, clock-, and scheduler-free.  It
implements a separately labelled, one-day competition contingency and returns
the existing :class:`~thetatrap.domain.StrategyEvaluation` contract so the
current immutable MLEG order builder can consume an approved candidate.

The profile does not use or synthesize earnings IV, term-structure, or expected
move evidence.  The corresponding legacy ``CondorCandidate`` fields are zeroed
sentinels until that shared order-facing domain type can be generalized.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
from thetatrap.strategy import round_credit_down


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

INTRADAY_PROFILE_ID = "sep3_intraday_theta_canary_v1"
INTRADAY_STRATEGY_NAME = "Intraday Theta Canary"
INTRADAY_STRATEGY_VERSION = "2.0-sep3-canary"
TRADE_DATE = date(2026, 9, 3)
EXPIRATION = date(2026, 9, 4)
PROFILE_OPEN_INTEREST_DATES = frozenset(
    {
        date(2026, 9, 2),
        date(2026, 9, 1),
        date(2026, 8, 31),
    }
)


@dataclass(frozen=True, slots=True)
class IntradayStrategyConfig:
    """Frozen safety policy for the Sep 3 competition contingency.

    Custom instances may tighten the policy for tests or diagnostics, but they
    cannot broaden the universe, accept older open interest, widen quotes, or
    increase the one-contract maximum loss.
    """

    trade_date: date = TRADE_DATE
    expiration: date = EXPIRATION
    allowed_symbols: tuple[str, ...] = ("QQQ", "SPY")
    wing_width: Decimal = Decimal("1.00")
    quantity: int = 1
    maximum_underlying_age_seconds: int = 10
    maximum_option_age_seconds: int = 60
    maximum_future_quote_skew_seconds: int = 2
    maximum_underlying_spread_fraction: Decimal = Decimal("0.002")
    maximum_short_spread: Decimal = Decimal("1.00")
    maximum_short_spread_fraction: Decimal = Decimal("0.25")
    maximum_wing_spread: Decimal = Decimal("1.00")
    maximum_wing_spread_fraction: Decimal = Decimal("0.50")
    minimum_short_open_interest: int = 500
    minimum_wing_open_interest: int = 100
    accepted_open_interest_dates: frozenset[date] = PROFILE_OPEN_INTEREST_DATES
    minimum_proposed_credit: Decimal = Decimal("0.20")
    maximum_midpoint_natural_gap: Decimal = Decimal("1.00")
    maximum_loss_dollars: Decimal = Decimal("80.00")

    def __post_init__(self) -> None:
        if self.trade_date != TRADE_DATE or self.expiration != EXPIRATION:
            raise ValueError("intraday profile is frozen to Sep 3 / Sep 4, 2026")
        normalized_symbols = tuple(symbol.strip().upper() for symbol in self.allowed_symbols)
        if (
            not normalized_symbols
            or len(set(normalized_symbols)) != len(normalized_symbols)
            or not set(normalized_symbols).issubset({"QQQ", "SPY"})
        ):
            raise ValueError("allowed_symbols must be a non-empty subset of QQQ and SPY")
        object.__setattr__(self, "allowed_symbols", normalized_symbols)
        if self.wing_width != ONE or self.quantity != 1:
            raise ValueError("intraday profile requires one contract with exact $1 wings")

        positive_bounded = {
            "maximum_underlying_age_seconds": (
                Decimal(self.maximum_underlying_age_seconds),
                Decimal("10"),
            ),
            "maximum_option_age_seconds": (
                Decimal(self.maximum_option_age_seconds),
                Decimal("60"),
            ),
            "maximum_future_quote_skew_seconds": (
                Decimal(self.maximum_future_quote_skew_seconds),
                Decimal("2"),
            ),
            "maximum_underlying_spread_fraction": (
                self.maximum_underlying_spread_fraction,
                Decimal("0.002"),
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
            "maximum_loss_dollars": (
                self.maximum_loss_dollars,
                Decimal("80.00"),
            ),
        }
        for name, (actual, hard_limit) in positive_bounded.items():
            if actual <= ZERO or actual > hard_limit:
                raise ValueError(f"{name} must be positive and no greater than {hard_limit}")

        if self.minimum_short_open_interest < 500:
            raise ValueError("minimum_short_open_interest cannot be lower than 500")
        if self.minimum_wing_open_interest < 100:
            raise ValueError("minimum_wing_open_interest cannot be lower than 100")
        if self.minimum_proposed_credit < Decimal("0.20"):
            raise ValueError("minimum_proposed_credit cannot be lower than 0.20")
        if self.minimum_proposed_credit >= self.wing_width:
            raise ValueError("minimum_proposed_credit must remain below wing width")

        accepted_dates = frozenset(self.accepted_open_interest_dates)
        if not accepted_dates or not accepted_dates.issubset(PROFILE_OPEN_INTEREST_DATES):
            raise ValueError(
                "accepted_open_interest_dates cannot exceed the prior T-1 through T-3 sessions"
            )
        object.__setattr__(self, "accepted_open_interest_dates", accepted_dates)


def evaluate_intraday_symbol(
    *,
    symbol: str,
    observed_at: datetime,
    underlying: UnderlyingQuote,
    option_chain: Sequence[OptionSnapshot],
    buying_power: Decimal,
    config: IntradayStrategyConfig | None = None,
) -> StrategyEvaluation:
    """Return the best eligible exact-$1 intraday condor for one ETF.

    Feasible structures are ranked by lowest aggregate relative spread, then
    highest natural credit.  Distance from spot and OCC symbols provide stable
    deterministic tie-breaks.  The function performs no time-window check;
    scheduling and one-shot authorization remain runtime responsibilities.
    """

    policy = config or IntradayStrategyConfig()
    _require_aware(observed_at)
    as_of = observed_at.astimezone(UTC)
    normalized_symbol = symbol.strip().upper()
    if normalized_symbol not in policy.allowed_symbols:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.CONTRACT_INELIGIBLE,
                    f"{normalized_symbol or '<empty>'} is outside the QQQ/SPY canary universe",
                )
            ],
        )

    underlying_failures = _validate_underlying(underlying, as_of, policy)
    if underlying_failures:
        return _rejected(normalized_symbol, underlying_failures)
    spot = underlying.midpoint

    chain = sorted(
        (
            snapshot
            for snapshot in option_chain
            if snapshot.contract.underlying_symbol.strip().upper() == normalized_symbol
            and snapshot.contract.expiration == policy.expiration
        ),
        key=lambda snapshot: snapshot.contract.symbol,
    )
    short_puts = [
        snapshot
        for snapshot in chain
        if snapshot.contract.right is OptionRight.PUT
        and snapshot.contract.strike < spot
    ]
    short_calls = [
        snapshot
        for snapshot in chain
        if snapshot.contract.right is OptionRight.CALL
        and snapshot.contract.strike > spot
    ]
    if not short_puts or not short_calls:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.SHORT_STRIKE_MISSING,
                    "expiration has no short put below spot and short call above spot",
                )
            ],
        )

    puts_by_strike = _by_strike(chain, OptionRight.PUT)
    calls_by_strike = _by_strike(chain, OptionRight.CALL)
    structures: list[CondorCandidate] = []
    attempted_failures: list[GateFailure] = []
    attempted_structures = 0
    geometry_exists = False
    for short_put in sorted(
        short_puts,
        key=lambda snapshot: (
            spot - snapshot.contract.strike,
            -snapshot.contract.strike,
            snapshot.contract.symbol,
        ),
    ):
        long_puts = puts_by_strike.get(
            short_put.contract.strike - policy.wing_width, ()
        )
        if not long_puts:
            continue
        for short_call in sorted(
            short_calls,
            key=lambda snapshot: (
                snapshot.contract.strike - spot,
                snapshot.contract.strike,
                snapshot.contract.symbol,
            ),
        ):
            long_calls = calls_by_strike.get(
                short_call.contract.strike + policy.wing_width, ()
            )
            if not long_calls:
                continue
            geometry_exists = True
            for long_put in long_puts:
                for long_call in long_calls:
                    attempted_structures += 1
                    candidate, failures = _evaluate_structure(
                        symbol=normalized_symbol,
                        observed_at=as_of,
                        spot=spot,
                        long_put=long_put,
                        short_put=short_put,
                        short_call=short_call,
                        long_call=long_call,
                        buying_power=buying_power,
                        config=policy,
                    )
                    if candidate is not None:
                        structures.append(candidate)
                    else:
                        attempted_failures.extend(failures)

    if structures:
        return StrategyEvaluation(
            symbol=normalized_symbol,
            candidate=rank_intraday_candidates(structures)[0],
        )
    if not geometry_exists:
        attempted_failures.append(
            GateFailure(
                GateCode.SYMMETRIC_WINGS_MISSING,
                "no short pair has protective puts and calls exactly $1 away",
            )
        )
    attempted_failures.append(
        GateFailure(
            GateCode.NO_VALID_CONDOR,
            "no liquid exact-$1 intraday condor passed "
            f"across {attempted_structures} priced structures",
        )
    )
    return _rejected(normalized_symbol, attempted_failures)


def evaluate_intraday_candidate_structure(
    *,
    original: CondorCandidate,
    observed_at: datetime,
    underlying: UnderlyingQuote,
    option_chain: Sequence[OptionSnapshot],
    buying_power: Decimal,
    config: IntradayStrategyConfig | None = None,
) -> StrategyEvaluation:
    """Revalidate the original four-leg candidate against fresh evidence.

    Revalidation is deliberately structure-bound.  A newly better-ranked
    condor must not invalidate an already authorized candidate, and it must
    never replace the authorized OCC symbols.  This evaluator therefore finds
    exactly one fresh snapshot for each original leg, verifies the immutable
    role/right/strike identity and $1 geometry, then reapplies every current
    underlying, contract, OI, quote, credit, loss, and buying-power gate.
    """

    if not isinstance(original, CondorCandidate):
        raise TypeError("original must be a CondorCandidate")
    policy = config or IntradayStrategyConfig()
    _require_aware(observed_at)
    as_of = observed_at.astimezone(UTC)
    normalized_symbol = original.symbol.strip().upper()
    if normalized_symbol not in policy.allowed_symbols:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.CONTRACT_INELIGIBLE,
                    f"{normalized_symbol or '<empty>'} is outside the QQQ/SPY canary universe",
                )
            ],
        )

    underlying_failures = _validate_underlying(underlying, as_of, policy)
    if underlying_failures:
        return _rejected(normalized_symbol, underlying_failures)
    spot = underlying.midpoint

    expected = (
        (original.long_put, LegRole.LONG_PUT, OrderSide.BUY, OptionRight.PUT),
        (original.short_put, LegRole.SHORT_PUT, OrderSide.SELL, OptionRight.PUT),
        (original.short_call, LegRole.SHORT_CALL, OrderSide.SELL, OptionRight.CALL),
        (original.long_call, LegRole.LONG_CALL, OrderSide.BUY, OptionRight.CALL),
    )
    identity_failures: list[GateFailure] = []
    original_symbols: set[str] = set()
    for leg, role, side, right in expected:
        contract = leg.snapshot.contract
        if (
            leg.role is not role
            or leg.side is not side
            or contract.right is not right
            or contract.underlying_symbol.strip().upper() != normalized_symbol
            or contract.expiration != policy.expiration
        ):
            identity_failures.append(
                GateFailure(
                    GateCode.CONTRACT_INELIGIBLE,
                    f"original {role.value} identity does not match the frozen profile",
                )
            )
        if contract.symbol in original_symbols:
            identity_failures.append(
                GateFailure(
                    GateCode.CONTRACT_INELIGIBLE,
                    "original candidate does not contain four unique OCC symbols",
                )
            )
        original_symbols.add(contract.symbol)
    if identity_failures:
        return _rejected(normalized_symbol, identity_failures)

    fresh_by_symbol: dict[str, list[OptionSnapshot]] = {}
    for snapshot in option_chain:
        fresh_by_symbol.setdefault(snapshot.contract.symbol, []).append(snapshot)
    fresh_legs: list[OptionSnapshot] = []
    for leg, role, _side, right in expected:
        original_contract = leg.snapshot.contract
        matches = fresh_by_symbol.get(original_contract.symbol, [])
        if len(matches) != 1:
            identity_failures.append(
                GateFailure(
                    GateCode.CONTRACT_INELIGIBLE,
                    f"fresh chain must contain exactly one {role.value} snapshot "
                    f"for {original_contract.symbol}",
                )
            )
            continue
        fresh = matches[0]
        contract = fresh.contract
        if (
            contract.underlying_symbol.strip().upper() != normalized_symbol
            or contract.expiration != policy.expiration
            or contract.right is not right
            or contract.strike != original_contract.strike
        ):
            identity_failures.append(
                GateFailure(
                    GateCode.CONTRACT_INELIGIBLE,
                    f"fresh metadata for {original_contract.symbol} changed its frozen identity",
                )
            )
        fresh_legs.append(fresh)
    if identity_failures:
        return _rejected(normalized_symbol, identity_failures)
    if len(fresh_legs) != 4:  # pragma: no cover - guarded by identity failures
        return _rejected(
            normalized_symbol,
            [GateFailure(GateCode.CONTRACT_INELIGIBLE, "fresh leg set is incomplete")],
        )

    long_put, short_put, short_call, long_call = fresh_legs
    long_put_strike = long_put.contract.strike
    short_put_strike = short_put.contract.strike
    short_call_strike = short_call.contract.strike
    long_call_strike = long_call.contract.strike
    if not short_put_strike < spot < short_call_strike:
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.SHORT_STRIKE_MISSING,
                    "fresh spot is no longer strictly between the frozen short strikes",
                )
            ],
        )
    if (
        short_put_strike - long_put_strike != policy.wing_width
        or long_call_strike - short_call_strike != policy.wing_width
        or not long_put_strike < short_put_strike < short_call_strike < long_call_strike
    ):
        return _rejected(
            normalized_symbol,
            [
                GateFailure(
                    GateCode.SYMMETRIC_WINGS_MISSING,
                    "frozen candidate no longer has exact symmetric $1 wings",
                )
            ],
        )

    candidate, failures = _evaluate_structure(
        symbol=normalized_symbol,
        observed_at=as_of,
        spot=spot,
        long_put=long_put,
        short_put=short_put,
        short_call=short_call,
        long_call=long_call,
        buying_power=buying_power,
        config=policy,
    )
    if candidate is None:
        return _rejected(
            normalized_symbol,
            [
                *failures,
                GateFailure(
                    GateCode.NO_VALID_CONDOR,
                    "the original exact-$1 structure failed fresh policy gates",
                ),
            ],
        )
    return StrategyEvaluation(symbol=normalized_symbol, candidate=candidate)


def rank_intraday_candidates(
    candidates: Iterable[CondorCandidate],
) -> tuple[CondorCandidate, ...]:
    """Rank canary candidates without using earnings-only IV fields."""

    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.aggregate_relative_spread,
                -candidate.natural_credit,
                max(
                    candidate.spot - candidate.short_put.snapshot.contract.strike,
                    candidate.short_call.snapshot.contract.strike - candidate.spot,
                ),
                (
                    candidate.spot - candidate.short_put.snapshot.contract.strike
                    + candidate.short_call.snapshot.contract.strike
                    - candidate.spot
                ),
                -candidate.short_put.snapshot.contract.strike,
                candidate.short_call.snapshot.contract.strike,
                candidate.symbol,
                tuple(leg.snapshot.contract.symbol for leg in candidate.legs),
            ),
        )
    )


def _evaluate_structure(
    *,
    symbol: str,
    observed_at: datetime,
    spot: Decimal,
    long_put: OptionSnapshot,
    short_put: OptionSnapshot,
    short_call: OptionSnapshot,
    long_call: OptionSnapshot,
    buying_power: Decimal,
    config: IntradayStrategyConfig,
) -> tuple[CondorCandidate | None, list[GateFailure]]:
    snapshots = (long_put, short_put, short_call, long_call)
    failures: list[GateFailure] = []
    for snapshot, short in zip(snapshots, (False, True, True, False), strict=True):
        failures.extend(
            _validate_leg(snapshot, observed_at, short=short, config=config)
        )
    if failures:
        return None, failures

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
    if natural_credit <= ZERO:
        failures.append(
            GateFailure(
                GateCode.CREDIT_TOO_LOW,
                "natural credit must be positive",
            )
        )
    if midpoint_credit <= ZERO:
        failures.append(
            GateFailure(GateCode.CREDIT_TOO_LOW, "midpoint credit must be positive")
        )

    penny_combination = all(snapshot.contract.ppind is True for snapshot in snapshots)
    proposed_credit, tick_size = round_credit_down(
        max(midpoint_credit, ZERO), penny_combination
    )
    if proposed_credit < config.minimum_proposed_credit:
        failures.append(
            GateFailure(
                GateCode.CREDIT_TOO_LOW,
                f"proposed credit must be at least {config.minimum_proposed_credit}",
            )
        )
    if proposed_credit >= config.wing_width:
        failures.append(
            GateFailure(
                GateCode.CREDIT_NOT_DEFINED_RISK,
                "proposed credit must remain below the $1 wing width",
            )
        )
    if midpoint_credit - natural_credit > config.maximum_midpoint_natural_gap:
        failures.append(
            GateFailure(
                GateCode.MIDPOINT_NATURAL_GAP_WIDE,
                "midpoint-to-natural credit gap exceeds policy",
            )
        )

    maximum_loss = (config.wing_width - proposed_credit) * HUNDRED
    if (
        maximum_loss <= ZERO
        or maximum_loss > config.maximum_loss_dollars
    ):
        failures.append(
            GateFailure(
                GateCode.MAXIMUM_LOSS_HIGH,
                f"maximum loss {maximum_loss} exceeds {config.maximum_loss_dollars}",
            )
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

    legs = (
        StrategyLeg(LegRole.LONG_PUT, OrderSide.BUY, long_put),
        StrategyLeg(LegRole.SHORT_PUT, OrderSide.SELL, short_put),
        StrategyLeg(LegRole.SHORT_CALL, OrderSide.SELL, short_call),
        StrategyLeg(LegRole.LONG_CALL, OrderSide.BUY, long_call),
    )
    spread_sum = sum((snapshot.quote.spread for snapshot in snapshots), ZERO)
    midpoint_sum = sum((snapshot.quote.midpoint for snapshot in snapshots), ZERO)
    aggregate_relative_spread = spread_sum / midpoint_sum
    reference_strike = min(
        (snapshot.contract.strike for snapshot in snapshots),
        key=lambda strike: (abs(strike - spot), strike),
    )
    net_delta = _net_position_delta(legs)

    return (
        CondorCandidate(
            symbol=symbol,
            observed_at=observed_at,
            spot=spot,
            front_atm_strike=reference_strike,
            back_atm_strike=reference_strike,
            expected_move=ZERO,
            expected_move_fraction=ZERO,
            front_atm_iv=ZERO,
            back_atm_iv=ZERO,
            iv_ratio=ZERO,
            long_put=legs[0],
            short_put=legs[1],
            short_call=legs[2],
            long_call=legs[3],
            wing_width=config.wing_width,
            natural_credit=natural_credit,
            midpoint_credit=midpoint_credit,
            proposed_credit=proposed_credit,
            tick_size=tick_size,
            maximum_profit=proposed_credit * HUNDRED,
            maximum_loss=maximum_loss,
            risk_budget=config.maximum_loss_dollars,
            quantity=config.quantity,
            aggregate_relative_spread=aggregate_relative_spread,
            net_delta=net_delta,
        ),
        [],
    )


def _validate_underlying(
    quote: UnderlyingQuote,
    observed_at: datetime,
    config: IntradayStrategyConfig,
) -> list[GateFailure]:
    failures: list[GateFailure] = []
    if quote.bid <= ZERO or quote.ask <= quote.bid:
        return [
            GateFailure(
                GateCode.UNDERLYING_QUOTE_INVALID,
                "underlying bid must be positive and ask must exceed bid",
            )
        ]
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
                "underlying relative spread exceeds 0.20% policy",
            )
        )
    return failures


def _validate_leg(
    snapshot: OptionSnapshot,
    observed_at: datetime,
    *,
    short: bool,
    config: IntradayStrategyConfig,
) -> list[GateFailure]:
    contract = snapshot.contract
    quote = snapshot.quote
    label = "short" if short else "wing"
    failures: list[GateFailure] = []
    if (
        not contract.tradable
        or contract.status != "active"
        or not contract.is_standard
        or contract.expiration != config.expiration
    ):
        failures.append(
            GateFailure(
                GateCode.CONTRACT_INELIGIBLE,
                f"{label} contract {contract.symbol} is not active/standard/tradable",
            )
        )
    if (
        contract.open_interest is None
        or contract.open_interest_date is None
        or contract.open_interest_date not in config.accepted_open_interest_dates
    ):
        failures.append(
            GateFailure(
                GateCode.OPEN_INTEREST_MISSING_OR_STALE,
                f"{label} contract {contract.symbol} lacks numeric OI dated T-1 through T-3",
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
                    f"{label} contract {contract.symbol} OI is below {minimum_oi}",
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
    absolute_limit = config.maximum_short_spread if short else config.maximum_wing_spread
    relative_limit = (
        config.maximum_short_spread_fraction
        if short
        else config.maximum_wing_spread_fraction
    )
    relative_spread = quote.relative_spread
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


def _by_strike(
    chain: Sequence[OptionSnapshot], right: OptionRight
) -> dict[Decimal, tuple[OptionSnapshot, ...]]:
    grouped: dict[Decimal, list[OptionSnapshot]] = {}
    for snapshot in chain:
        if snapshot.contract.right is right:
            grouped.setdefault(snapshot.contract.strike, []).append(snapshot)
    return {
        strike: tuple(sorted(snapshots, key=lambda item: item.contract.symbol))
        for strike, snapshots in grouped.items()
    }


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
    age = observed_at - timestamp.astimezone(UTC)
    return (
        -timedelta(seconds=maximum_future_skew_seconds)
        <= age
        <= timedelta(seconds=maximum_age_seconds)
    )


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
