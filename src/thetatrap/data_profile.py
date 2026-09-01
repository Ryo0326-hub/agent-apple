"""Fail-closed Alpaca market-data profile used by the competition worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


BASIC_INDICATIVE_PROFILE_ID = "alpaca_basic_iex_indicative_v1"
BASIC_STOCK_FEED = "iex"
BASIC_OPTION_FEED = "indicative"
BASIC_INDICATIVE_LIMITATIONS = (
    "IEX stock quotes are not consolidated SIP coverage.",
    "Indicative option quotes are not consolidated OPRA quotes.",
    "Paper fills are simulated and do not establish live execution quality.",
)


@dataclass(frozen=True, slots=True)
class MarketDataProfile:
    """Public-safe identity and limitations for one approved feed combination."""

    profile_id: str
    provider: str
    plan: str
    stock_feed: str
    option_feed: str
    limitations: tuple[str, ...]

    def status(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "plan": self.plan,
            "stock_feed": self.stock_feed,
            "option_feed": self.option_feed,
            "consolidated_stock_quotes": False,
            "consolidated_option_quotes": False,
            "limitations": list(self.limitations),
        }


ALPACA_BASIC_INDICATIVE = MarketDataProfile(
    profile_id=BASIC_INDICATIVE_PROFILE_ID,
    provider="alpaca",
    plan="basic",
    stock_feed=BASIC_STOCK_FEED,
    option_feed=BASIC_OPTION_FEED,
    limitations=BASIC_INDICATIVE_LIMITATIONS,
)


def require_basic_indicative_profile(
    *, stock_feed: str, option_feed: str
) -> MarketDataProfile:
    """Return the sole competition profile or reject the process configuration."""

    if stock_feed != BASIC_STOCK_FEED:
        raise ValueError(
            f"ALPACA_STOCK_FEED must exactly equal {BASIC_STOCK_FEED} "
            "for the competition data profile"
        )
    if option_feed != BASIC_OPTION_FEED:
        raise ValueError(
            f"ALPACA_OPTION_FEED must exactly equal {BASIC_OPTION_FEED} "
            "for the competition data profile"
        )
    return ALPACA_BASIC_INDICATIVE
