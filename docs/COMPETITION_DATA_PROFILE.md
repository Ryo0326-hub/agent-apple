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

These limitations do not relax any strategy rule. Underlying and option quote
freshness, bid/ask validity, spread, open-interest, structure, defined-loss,
one-contract, one-shot authorization, and broker-order controls remain unchanged.

## Deterministic short-strike fallback

Basic indicative data can omit current open interest or publish an unusable
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

Before tomorrow's competition run, `check-config` and `preflight` must both show
the expected profile ID and exact feeds. Do not change the feed variables to try
to manufacture an eligible candidate.
