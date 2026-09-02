# Competition market-data profile

ThetaTrap runs the competition worker with exactly one reviewed Alpaca Basic
market-data profile:

```dotenv
ALPACA_STOCK_FEED=iex
ALPACA_OPTION_FEED=indicative
```

Both values have safe defaults and are fail-closed. Any other value prevents
configuration from loading before the worker can collect data or submit an
order. The same option-feed setting is used for entry screening and deterministic
exit repricing.

The profile identity is `alpaca_basic_iex_indicative_v1`. Its public-safe status
payload is stored in worker heartbeat detail and strategy-run context, and is
also returned by `check-config`, `preflight`, account discovery, MCP smoke, and
agent smoke where applicable. This makes the deployed feed choice auditable
without exposing credentials.

## Limitations

- IEX stock quotes are not consolidated SIP coverage.
- Indicative option quotes are not consolidated OPRA quotes.
- Alpaca paper fills are simulated and do not establish live execution quality.

These limitations do not permit ad hoc gate changes during a run. Each deployed
strategy profile freezes its own quote, open-interest, structure, defined-loss,
one-contract, one-shot authorization, and broker-order controls before its
entry window.

## Observed September 1–2 result

The official worker completed 176 earnings-candidate evaluations on September 1
and 138 on September 2. None produced a complete eligible four-leg structure;
there were zero broker order attempts and zero fills. The repeated blockers were
consistent with the declared profile: non-consolidated underlying spreads,
open-interest dates that did not meet the earnings profile's prior-session rule,
and stale, invalid, or wide quotes across one or more option legs.

This evidence does not show that the earnings thesis is unprofitable. It shows
that the strategy was not executable under its frozen gates, symbols, timing,
and available Basic-feed observations. The worker correctly retained `$100,000`
equity instead of inventing missing data or forcing a trade.

## September 3 Intraday Theta Canary

The final-day canary is a separate profile, `sep3_intraday_theta_canary_v1`,
and is valid only on September 3. It is not automatically activated by an
earnings rejection.

- Rank QQQ and SPY by complete live quote quality; use September 4 options.
- Evaluate only one-contract, symmetric `$1`-wide iron condors.
- Require at least `$0.20` credit, limiting defined loss to at most `$80`.
- Require underlying quotes no older than 10 seconds and option quotes no older
  than 60 seconds; all four quotes must be positive, uncrossed, and inside the
  profile's spread bounds. Underlying spread is at most 0.20% of midpoint;
  short-leg spreads are at most `$1.00` and 25% of midpoint; wing spreads are
  at most `$1.00` and 50% of midpoint.
- Require numeric open interest of at least `500` on each short and `100` on
  each wing. The metadata date may be no more than three prior trading sessions
  old, reflecting the cadence observed in the Basic feed.
- Re-scan market evidence every worker cycle, while allowing an eligible Qwen
  review cycle no more often than once every five minutes.
- Enter only from 09:45–10:45 ET, cancel an unresolved entry by 10:50, begin a
  same-day exit at 15:15, advance to the full-wing exit limit at 15:25, and
  target flatness by 15:45.

Within the canary's metadata rules, the only stale-data allowance is the
open-interest *date tolerance*. Missing/non-numeric open interest, low numeric liquidity, stale or crossed
quotes, invalid structure, insufficient credit, or risk above `$80` still
rejects the candidate. A paper fill remains simulated and any nonzero P&L is a
single observed competition result, not proof of repeatable alpha.

## Earnings-profile deterministic short-strike fallback

For the primary earnings profile, Basic indicative data can omit current open interest or publish an unusable
quote at one listed strike while farther-out contracts remain valid. ThetaTrap
therefore does not treat the first strike outside the expected move as the only
possible short leg. It enumerates puts at or below the downside threshold and
calls at or above the upside threshold, then evaluates short pairs nearest to
the thresholds first. Pair ordering is deterministic: maximum outward distance,
side-to-side distance imbalance, total outward distance, then strike and OCC
symbol tie-breaks.

Each pair must still pass the unchanged short-leg open-interest, freshness,
bid/ask, and spread gates before equal-width protective wings are considered.
Each complete structure must still pass the unchanged credit, midpoint-natural
gap, delta-when-available, buying-power, defined-loss, and one-contract gates.
If no complete structure passes, the result remains `NO_VALID_CONDOR`; the
worker does not force a trade.

Before the September 3 competition run, `check-config` and `preflight` must both show
the expected profile ID and exact feeds. Do not change the feed variables to try
to manufacture an eligible candidate.
