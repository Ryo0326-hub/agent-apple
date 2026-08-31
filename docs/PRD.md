# ThetaTrap Product Requirements Document

- **Version:** 1.2
- **Status:** Implementation complete; market-hours canary, cloud release, and official result pending
- **Target:** Alpaca AI Trading Agents Hackathon, August 28–September 4, 2026
- **Official trading/P&L window:** August 31–September 4, 2026
- **Trading environment:** Alpaca paper trading only
- **Deployment:** DigitalOcean Droplet

## 1. Product summary

ThetaTrap is a bounded autonomous paper-trading agent that looks for unusually rich earnings-event option premium and may sell one defined-risk iron condor before the event. Featherless-hosted Qwen operates as a genuine MCP tool-using agent: it discovers and calls tools from Alpaca's official MCP server, reviews qualitative event risk, and must invoke the pre-authorized Alpaca option-order tool for an entry to occur. Deterministic Python owns every calculation, risk limit, immutable order intent, and final policy gate.

In plain English: the agent tries to collect expensive option premium before a confirmed earnings announcement, but only when the market appears to be pricing a large move, quotes are usable, and the worst-case loss is below $500. The product is deliberately tuned to seek at least one bounded official trade during the competition window; `NO_TRADE` remains the safe outcome when no candidate satisfies the non-negotiable gates.

This is a strategy hypothesis, not a proven profitable system. Alpaca Basic option quotes are indicative and Alpaca paper fills do not reproduce real execution.

## 2. Product objective

Deliver a credible end-to-end hackathon product that:

1. Runs continuously on a DigitalOcean Droplet without the developer's laptop.
2. Autonomously discovers, evaluates, enters, monitors, and exits eligible paper option trades.
3. Makes the AI's role visible without allowing the model to bypass deterministic safeguards.
4. Produces a judge-readable audit trail linking every decision to market data, tool calls, policy checks, orders, fills, and account equity.
5. Is small enough to implement, test, deploy, and demonstrate in one day.
6. Uses Alpaca's official MCP server as the sole Alpaca data and trading boundary; no direct Alpaca SDK or REST fallback is allowed.

## 3. Success definition

### Product success

- The cloud worker remains healthy and resumes safely after a restart.
- The worker launches and supervises a pinned official Alpaca MCP v2 server, verifies its discovered tool schemas at startup, and records every MCP call and result.
- Every scheduled cycle ends in a valid canonical strategy state with an explicit reason and timestamp.
- No invalid model response can create an order.
- A valid entry requires an auditable Qwen call to the official `place_option_order` MCP tool with the exact pre-authorized order intent.
- At most one logical entry chain and one filled four-leg entry exist for an event day.
- An open position is automatically closed the next morning.
- The dashboard shows system health, account equity, candidate reasoning, policy results, order status, position status, and realized P&L.

### Competition success

- The submitted fresh $100,000 paper account is used only for the official run.
- Total account equity is timestamped throughout the official window.
- The demo visibly proves autonomy, Alpaca integration, deterministic risk enforcement, failure handling, and explainability.
- The submission includes a judge-accessible repository, demo video, pitch deck, one-page AI/risk/infrastructure summary, and final run report.
- All pre-hackathon code, infrastructure, templates, and boilerplate are disclosed.
- The LabLab entry includes project title, short and long descriptions, technology/category tags, and a cover image.
- Every participant is enrolled in the event and belongs to the submitted LabLab team, including a solo entrant.
- The pitch explicitly maps profitability to final account equity, creativity to the AI event-risk veto and inspectable tool trace, autonomy to the cloud lifecycle, and robustness to deterministic policy and restart recovery.
- **Execution objective:** complete at least one official filled entry and exit so the account records a non-zero realized P&L. This is a competition objective, not a safety invariant or profit guarantee.

### Confidence statement

- **Engineering and safety confidence:** supported by the implemented acceptance suite, but not complete until the controlled first competition-order canary and cloud restart proof pass.
- **Trade-occurrence confidence:** uncalibrated. Seven events are eligible for screening, but quote, liquidity, AI-veto, policy, and fill gates can still produce no filled trade.
- **Profitability confidence:** low until causally valid trades close. Increasing the chance of execution does not guarantee positive P&L, and one competition-week result cannot validate long-run expectancy.

### Zero-trade risk and measurement

Earlier planning ranges for the chance of no fill were judgment estimates, not calibrated backtests, and are not used as public evidence. Version 1.1 freezes seven eligible events and two explicit exclusions. It may review up to two ranked candidates sequentially before broker dispatch, but permits one initial broker entry attempt per strategy date. A model veto or pre-dispatch deterministic failure may advance to the second opportunity; a broker dispatch consumes the day's opportunity even if it receives no fill.

The implementation measures the outcome instead of presenting a planning estimate as fact. Read-only shadow runs record which gate fails for each symbol and sample. The final report records submitted orders, fills, flatness, and observed account equity. No production rule is loosened ad hoc after seeing a desired outcome.

`At least one fill` and `positive P&L` are different goals. More candidates can reduce the chance of a zero account change caused by inactivity, but cannot ensure the earnings move stays inside the shorts or that the trade closes profitably. ThetaTrap seeks a positive expected-value setup; it never promises a positive competition result.

## 4. Scope

### In scope

- Alpaca paper account, market data, option chains, news, clock/calendar, orders, positions, and account activities.
- A curated 2026 earnings-event universe:
  - Tuesday, September 1 after close: PANW, MDB, CRDO, and GTLB.
  - Wednesday, September 2 after close: AVGO, SNOW, and AI.
  - DELL is excluded as `RELEASE_TIME_AMBIGUOUS`.
  - NTAP is excluded as `REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE` because neither required weekly expiration is listed; the nearest observed standard expiration is September 18.
- September 4, 2026 is the only tradable expiration; September 11 is queried read-only for term-structure analysis.
- One iron condor maximum per event day and one contract maximum per trade.
- Featherless native tool calling with Qwen connected through an application MCP client to Alpaca's official MCP v2 server.
- Deterministic selection, risk policy, execution, monitoring, exit, reconciliation, and journaling.
- A private Streamlit operator UI with kill-switch control plus a separately isolated public read-only judge view.
- Replay fixtures for a market-closed demo.
- One DigitalOcean Droplet deployment using Docker Compose.

### Explicitly out of scope

- Real-money or live Alpaca trading.
- Direct Alpaca SDK/REST calls or a non-MCP broker fallback.
- Naked options, rolling, averaging down, discretionary manual orders, or multiple simultaneous positions.
- A broad or dynamically scraped earnings universe, or any runtime event not frozen to a first-party source.
- Directional news or sentiment trading.
- Strategy generation or numeric risk decisions by the LLM.
- Model training, complex backtesting infrastructure, portfolio optimization, or a profitability claim.
- OPRA subscription, Redis, Postgres, a queue, Kubernetes, microservices, or a custom frontend.
- Automatic liquidation after assignment, exercise, or a broken multi-leg position. These abnormal states require audited `RISK_OFF` plus manual broker intervention; the agent must not improvise untested stock or single-leg orders.
- Public mutation controls, a general multi-user product, mobile-app work, or broker support beyond Alpaca.

## 5. Official run schedule

The official P&L window begins Monday, August 31 at 09:30 ET and ends with the Friday, September 4 snapshot. All scheduling uses `America/New_York` and is cross-checked against Alpaca's clock and calendar.

| Date | Competition behavior |
| --- | --- |
| Mon Aug 31 | Competition account read-only observation. No entry. Validate the production worker and audit trail. Keep the separate testing account stopped/disarmed and fixture/replay-only. |
| Tue Sep 1, 14:50–15:40 ET | Evaluate PANW, MDB, CRDO, and GTLB. Qwen may review up to two eligible candidates in deterministic rank order before broker dispatch. Permit one initial broker entry attempt and at most one filled position; cancel any unfilled entry by 15:45. DELL remains excluded. |
| Wed Sep 2, 09:45–09:53 ET | Autonomously exit Tuesday's position. If the account is flat, evaluate AVGO, SNOW, and AI from 14:50–15:40 using the same two-review/one-attempt rule. NTAP remains excluded. |
| Thu Sep 3, 09:45–09:53 ET | Autonomously exit Wednesday's position. No further entries. Remain flat through the competition snapshot. |
| Fri Sep 4, 09:30 ET | Capture and export the final total-equity snapshot. Retain both the Thursday EOD and Friday 09:30 records. No trading. |

The worker ticks every 60 seconds and evaluates idempotent time windows; it does not depend on a single cron instant. On restart, it reconciles Alpaca state and immediately handles an overdue exit.

## 6. Trading strategy

### 6.1 Thesis

Earnings can create a front-expiration implied-volatility premium that collapses after the announcement. ThetaTrap attempts to sell that premium while defining loss on both sides. The thesis is accepted only when the front-week volatility bump, expected move, liquidity, credit, and risk gates all pass.

The September 4 U.S. Employment Situation release may keep some index-wide volatility embedded in the September 4 expiration, so the next-morning earnings IV crush may be smaller than expected. This limitation remains visible in the run report.

### 6.2 Event source

Events are committed before the run in `config/events.yaml`, with symbol, event date, release timing, optional conference-call time, eligibility status, exclusion reason, and an investor-relations source URL. The runtime does not invent an exact release timestamp or trust an unverified calendar scraper. The frozen eligible set is PANW, MDB, CRDO, and GTLB on Tuesday and AVGO, SNOW, and AI on Wednesday. DELL is excluded as `RELEASE_TIME_AMBIGUOUS`: its first-party notice provides a 16:30 ET conference-call time but insufficient evidence for the exact results-release timing required by policy. NTAP is excluded as `REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE`: neither September 4 nor September 11 is listed, and the nearest observed standard expiration is September 18. The evidence is indexed in `docs/EVENT_SOURCES.md`.

### 6.3 Candidate construction

For each scheduled symbol:

1. Fetch through the official Alpaca MCP server a fresh IEX stock quote, both September 4 and September 11 chains, and paginated option-contract metadata joined by option symbol.
2. Define spot as the IEX bid/ask midpoint. Reject a missing, crossed, older-than-10-seconds quote or a stock spread above 0.20% of spot.
3. For each expiration, identify the strike nearest spot that has both a call and put. An equal-distance tie uses the lower strike.
4. Define ATM IV independently for each expiration as the arithmetic mean of the valid call and put IV at that expiration's selected ATM strike.
5. Calculate the expected dollar move from the September 4 ATM call midpoint plus put midpoint at the same strike.
6. Select the nearest short put and short call whose strikes are just outside `1.00 × expected_move`. Delta is a diagnostic, not a strike-selection gate.
7. Search outward for the narrowest equally spaced, listed protective wings no wider than `$5` that satisfy liquidity, credit, and risk requirements.
8. Require equal wing widths. If valid Greeks exist for all legs, require absolute estimated net position delta `<= 0.15`; missing indicative Greeks alone do not reject an otherwise valid structure.
9. Calculate maximum loss as `(wing_width - proposed_credit) × 100`.

Contract metadata supplies tradability, multiplier/deliverable, open interest, and open-interest date. Open interest must be dated no earlier than the previous trading day. Missing or stale metadata is a hard rejection. A short leg requires open interest of at least `50`; a protective wing requires at least `10`. If a complete symmetric structure cannot be constructed, that symbol is rejected and screening continues to the next scheduled symbol.

### 6.4 Premium and liquidity gates

Every candidate must pass all gates:

- Front-week ATM IV divided by September 11 ATM IV is at least `1.10`. This is an uncalibrated front-volatility-premium hypothesis, not proof of an edge.
- The ATM expected move is between 5% and 18% of spot. This is a sanity/risk gate, not an assertion that the move forecast is correct.
- Every leg is tradable, standard 100-share deliverable, and has bid, ask, timestamp, current open-interest metadata, and a contract status that permits trading.
- Bid is positive; quote is neither crossed nor locked.
- The underlying quote age is at most 10 seconds and each option quote age is at most 60 seconds. No artificial cross-leg two-second synchronization rule is used with the Basic indicative feed.
- Each short-leg bid/ask spread is at most `$1.00` and 25% of midpoint.
- Each long-wing bid/ask spread is at most `$1.00` and 50% of midpoint.
- The ATM call and put used for expected move or IV ratio each have a spread at most 25% of midpoint and an option quote age of at most 60 seconds.
- Short-leg open interest is at least `50`; wing open interest is at least `10`.
- Conservative natural credit, calculated as short bids minus long asks, and the submitted credit must each be at least `max($0.20, 10% × wing_width)`.
- Submitted credit must be less than wing width; there is no arbitrary fixed maximum-credit ceiling.
- The midpoint-to-natural credit gap is at most `$1.00`.
- One-contract maximum loss at the proposed limit credit is at most `$500` and available buying power is sufficient.
- A complete fresh MCP snapshot immediately before dispatch must still pass every hard gate.

Option trade prints are not used for signals or execution because the Basic feed's option trades may be delayed.

### 6.5 Candidate ranking

Rank all eligible symbols for the day deterministically by:

1. Highest front-week/back-week ATM IV ratio.
2. Lowest aggregate relative bid/ask spread across the four legs.
3. Highest conservative credit divided by wing width.
4. Alphabetical symbol as a stable final tie-breaker.

The worker processes this ranked queue one candidate at a time and reviews no more than two candidates per strategy date. It freezes an immutable, one-contract MCP order intent for the current candidate, then lets Qwen investigate and either reject it or issue the exact pre-authorized `place_option_order` MCP call. A model veto, insufficient evidence, invalid call, timeout before mutation dispatch, or deterministic revalidation failure may advance to the second candidate while time remains. Once any initial broker mutation is dispatched, the date's sole entry attempt is consumed; terminal zero fill does not advance to another candidate. Repricing and cancellation remain within the same durable logical order chain.

## 7. AI agent behavior

### 7.1 Model

- Primary: `Qwen/Qwen3-Coder-Next` on Featherless.
- Fallback: `Qwen/Qwen3-32B`, used only when the primary times out, returns 5xx, or is unavailable. This model was retained after a live Featherless tool-call compatibility check.
- Temperature: `0`.
- At startup, the application discovers the pinned MCP server's tool schemas and converts the approved subset to Featherless/OpenAI function definitions without hand-retyping them.
- Every generated tool argument is validated against the discovered JSON schema with unknown fields rejected before any MCP dispatch.
- If both models fail or output is invalid before dispatch, reject the current candidate and advance the ranked queue while time remains; if none remain, return `NO_TRADE`.

Before deployment, the two models must pass a small fixed evaluation covering valid tool calls, malformed data, no-trade decisions, risk-boundary cases, and adversarial text in news.

### 7.2 Model responsibility

Qwen:

- Calls approved official Alpaca MCP read tools to understand the account, clock, orders, positions, and recent news, and local read tools for the frozen event and candidate.
- Classifies qualitative event risk as `VETO` or `INSUFFICIENT_EVIDENCE` with finite reason codes, or expresses `ALLOW` by issuing the official `place_option_order` MCP call for the supplied immutable entry draft.
- May issue only that exact pre-authorized entry call. The local policy gateway revalidates and authorizes it before forwarding it to Alpaca's MCP server.
- Produces a short judge-readable explanation.

Qwen does not:

- Calculate or change strikes, payoff, quantity, prices, risk limits, order class, time in force, leg sides, client order ID, or signed MCP credit price.
- Change the event universe or strategy configuration.
- Access credentials or raw broker clients.
- Call MCP replace, cancel, close, exercise, stock-order, crypto-order, or bulk-mutation tools.
- Override a failed policy gate.

Natural-language `ALLOW` is not executable. Only a schema-valid `place_option_order` call whose arguments exactly equal the pending pre-authorized intent can advance to `SUBMITTING`. The gateway, not the model, holds final authority; any mismatch is rejected before reaching the official MCP server.

### 7.3 Qualitative veto

Recent Alpaca news is treated as untrusted data, never as instructions. Qwen uses only these finite veto reason codes: `EVENT_TIME_CONFLICT`, `RESULTS_ALREADY_RELEASED`, `PENDING_MA_OR_DELIVERABLE_CHANGE`, `BANKRUPTCY`, `ACCOUNTING_RESTATEMENT`, `TRADING_HALT`, `REGULATORY_OR_LEGAL_BINARY_EVENT`, `UNEXPECTED_EXECUTIVE_DEPARTURE`, and `INSUFFICIENT_EVIDENCE`. A completed acquisition mentioned as historical context is not an M&A veto. Each veto must cite timestamped evidence from the supplied data.

The model can choose whether to act on an already eligible bounded candidate, but cannot widen risk or authorize a deterministically ineligible candidate.

### 7.4 MCP and local tool surface

The worker is an MCP client. It launches one long-lived, pinned `alpaca-mcp-server==2.3.0` child process over `stdio`, calls `initialize` and `list_tools`, verifies the expected tool names and schemas, and stores a schema hash before arming entries. The child receives only paper credentials with `ALPACA_PAPER_TRADE=true` and these coarse toolsets: `account,trading,assets,stock-data,options-data,news`.

Alpaca's `trading` toolset is intentionally broader than this application needs, so the host exposes only this discovered official subset to Qwen:

- `get_account_info`
- `get_account_config`
- `get_clock`
- `get_orders`
- `get_all_positions`
- `get_news`
- `place_option_order`, restricted to the one exact pending entry intent

Local application tools presented in the same Featherless loop are `list_verified_events`, `get_candidate`, `get_run_summary`, and `record_candidate_rejection`. They cannot contact Alpaca or mutate broker state.

The deterministic worker may additionally call approved official MCP tools under a `system` principal for data collection and safety lifecycle work, including account activities, option contracts/chains/quotes, stock quotes, order lookup by ID or client ID, replacement, cancellation, and the exact opposite-side MLeg exit. Neither Qwen nor the UI receives those mutation capabilities.

Every Alpaca interaction—including reads, entry, repricing, cancellation, reconciliation, and exit—must traverse `MCP ClientSession.call_tool`; direct SDK or REST access is prohibited. The audit trace records the principal, official MCP tool name, schema hash, arguments, policy decision, result, and server security metadata. MCP responses and news remain untrusted data.

Featherless returns function arguments as JSON text. The bridge parses that outer JSON exactly once, requires `legs` to be a native array of four objects, validates it against the discovered schema, and passes the resulting Python list to `ClientSession.call_tool`. It never forwards a JSON-encoded string as the `legs` value.

Before any entry dispatch, the policy gateway re-fetches account, clock, orders, positions, and quotes through MCP and reruns all hard gates. It then durably records the accepted tool call and `SUBMITTING` before forwarding it. A veto, rejection, invalid call, or pre-dispatch model timeout advances safely without a broker call. Once forwarding has been attempted, a later model timeout or invalid final prose cannot revert the state to `NO_TRADE`; broker reconciliation owns the outcome.

### 7.5 Known MCP integration risk

Alpaca issue `#97` reports that Claude Desktop/Cowork serialized multi-leg `legs` as a string, causing `place_option_order` validation to fail. ThetaTrap owns its MCP bridge and passes a native parsed array. Contract tests and the exact pinned server/model probe must pass before arming; the first competition-account entry is the controlled live serialization canary. If it fails, the team may apply and disclose a minimal tested patch to the official MCP server; it may not silently bypass MCP with direct REST or an SDK.

## 8. Risk and execution policy

### 8.1 Entry admission gates

Competition mode is hard-coded to Alpaca paper trading. Opening new exposure requires:

- `EXPECTED_ALPACA_ACCOUNT_ID` matches the broker response.
- Options trading level is at least 3.
- On first admission, equity is $100,000, with no positions or open orders.
- On every restart, account ID still matches and all broker state is reconciled.
- The official window has begun and the current time is inside an allowed action window.
- No existing option position, working entry, unresolved order, assignment, or kill switch exists.

Testing and competition credentials live in separate environment files and must never be mixed or committed.

These gates restrict entries only. Once the expected paper account is confirmed, reconciliation, order cancellation, and position-reducing exits remain authorized outside entry windows and while the kill switch is active. Missing data while flat produces `NO_TRADE`; missing or conflicting data with exposure produces `RISK_OFF` or `EXIT_PENDING`, never passive inaction.

### 8.2 Portfolio limits

- Defined-risk iron condors only.
- One open position maximum.
- One contract maximum.
- Maximum theoretical loss per trade: `$500` or 0.5% of initial equity, whichever is lower.
- Quantity is `min(1, floor(0.005 × initial_equity / maximum_loss_per_contract))`; quantity zero means `NO_TRADE`.
- Account kill threshold: total equity at or below `$99,000`.
- No re-entry, roll, adjustment, averaging, or second initial broker entry attempt on the same strategy date. A terminally canceled zero-fill order does not release the consumed daily attempt.
- While flat, `NO_TRADE` is the required response when any entry input is missing or ambiguous.
- Activating the kill switch atomically persists the new-entry block and `RISK_OFF` state. A later worker cycle cancels only the broker order matched to the durable working-entry chain and reconciles any cancel/fill race. If the four signed option positions still exactly match the entry intent, the normal atomic exit can start at the next broker-permitted time. The kill switch never blocks that position-reducing exit.

### 8.3 Entry execution

- Use the pinned official Alpaca MCP v2 server's `place_option_order` tool and an atomic four-leg `MLEG` `DAY` limit order. There is no direct Alpaca SDK or REST execution path.
- Static execution flags do not authorize an initial entry by themselves. Each strategy date requires one durable authorization bound to the exact environment and account UUID. It expires at `stop_new_orders` and is atomically consumed with the persisted `SUBMITTING` transition and sequence-zero order attempt before the MCP mutation. Repricing, cancellation, reconciliation, and the mandatory exit do not consume another entry authorization.
- Deterministic code creates and persists an immutable `OrderIntent` containing the exact four OCC legs, sides, quantity `1`, order class, time in force, limit, and deterministic client order ID. Qwen must copy that draft exactly into its official MCP tool call; it cannot author order economics.
- Represent credit and debit as positive values in domain math. Alpaca MCP v2.3.0 expects a negative `limit_price` for a credit and a positive value for a debit, so the gateway creates and signs the negative wire value in the pre-authorized entry intent and requires exact equality from Qwen.
- A mutation must be the sole tool call in its model turn and may occur only after the required MCP read evidence has returned. Parallel or mixed read-and-mutate batches are rejected.
- Start at the calculated midpoint credit, rounded down to the broker-valid tick.
- Reprice by `$0.05` toward the natural credit no more than once every 30 seconds, normalizing every replacement down to the broker-valid tick.
- Never submit below `max($0.20, 10% × wing_width)`, at a credit greater than or equal to wing width, or at a price that raises maximum loss above `$500`; accept and audit any broker price improvement.
- Repricing and cancellation are deterministic `system`-principal MCP calls, not additional model decisions.
- Cancel all unfilled entry orders by 15:45 ET. After confirmed terminal zero-fill cancellation, record `NO_TRADE` for the date; do not offer another candidate because the sole broker attempt has been consumed.
- Never use a market order for entry.
- Verify the MCP credit-price sign and exact `MLEG` schema through the pinned contract and regression tests before arming; observe the first competition-account entry as the controlled live canary.

When Qwen calls `place_option_order`, the policy gateway validates exact intent equality, performs the fresh MCP reads and complete policy recheck, persists the accepted agent action and `SUBMITTING`, and only then forwards the unchanged call to the official server. The initial `client_order_id` is deterministic from account, event date, symbol, expiration, and strategy version. Repricing stays in one serialized logical order chain: use MCP `replace_order_by_id` when supported; otherwise use MCP cancellation, confirm terminal cancellation, and create a sequenced replacement ID. Never allow parallel entry orders.

Any timeout or session failure after mutation dispatch is ambiguous, not `NO_TRADE`. Allow at least 35 seconds for a mutation call because the pinned server's internal Alpaca HTTP timeout is 30 seconds, then query `get_order_by_client_id` through MCP before any retry. Broker truth controls all post-dispatch state.

At 15:45, `CANCEL_PENDING` cannot become `FLAT` until broker cancellation and positions are reconciled. A cancel/fill race becomes `POSITION_OPEN` and schedules the mandatory exit.

### 8.4 Exit execution

Beginning at 09:45 ET the next morning, close the complete position regardless of P&L. The deterministic `system` principal uses official MCP calls; the exit does not depend on Qwen. Persist `EXIT_SUBMITTING` and use a distinct deterministic exit client ID. Start one atomic opposite-side MLeg limit order at the midpoint debit rounded up to the valid tick, then reprice by `$0.05` every 30 seconds toward the natural close. Normalize every intact-condor exit replacement up to the broker-valid tick. By 09:53, use the most aggressive permitted limit, capped at the actual wing width while the condor remains intact. Query by client ID through MCP after an ambiguous timeout before retrying. Market orders are never used.

The position must be confirmed flat by 09:55 if the broker permits execution. An exit order reporting `filled` closes its order chain but remains `EXIT_PENDING` until a later fresh Alpaca snapshot independently reports zero positions and zero open orders. If flatness is not confirmed, keep `RISK_OFF` active and publish a persistent critical dashboard/audit state rather than falsely reporting success. Bounded repricing continues only while the durable intact-condor exit remains working; a terminal exit order or broken position requires manual broker intervention.

The worker checks account activities and exact signed positions through MCP for assignments, exercises, corporate actions, partial legs, or unmatched legs. Any such abnormal state activates the kill switch, persists the evidence, prohibits new exposure, and remains `RISK_OFF` until a human resolves the position at Alpaca and MCP reconciliation confirms flatness. It reconciles durable order chains, cancels only a matched working entry, and never mistakes a position-reducing exit for an entry cancellation. Automatic stock or single-leg liquidation is intentionally excluded from this competition build because it has not been safely validated.

## 9. Product flow

The canonical strategy states are `DISCOVERING`, `SCREENING`, `AI_REVIEW`, `POLICY_CHECK`, `SUBMITTING`, `ORDER_PENDING`, `CANCEL_PENDING`, `POSITION_OPEN`, `EXIT_SUBMITTING`, `EXIT_PENDING`, `RISK_OFF`, `FLAT`, `NO_TRADE`, and `ERROR`. Broker order statuses are stored separately and never substituted for strategy state.

```text
DISCOVERING -> SCREENING -> AI_REVIEW -> POLICY_CHECK -> SUBMITTING
                    |           |              |              |
                    +-----------+--------------+---------> NO_TRADE

SUBMITTING -> ORDER_PENDING -> POSITION_OPEN -> EXIT_SUBMITTING
                   |                  |                  |
             CANCEL_PENDING       RISK_OFF         EXIT_PENDING -> FLAT
```

Only the worker can enter `SUBMITTING` or `EXIT_SUBMITTING`. All transitions and their evidence are persisted before an external broker call. `ERROR` while exposed immediately transitions to `RISK_OFF`.

## 10. Technical architecture

One repository and one Docker image run in three application roles behind Caddy:

```text
DigitalOcean Droplet
|-- worker (exactly one replica)
|   |-- 60-second scheduler and state machine
|   |-- Featherless Qwen tool loop and MCP-schema bridge
|   |-- MCP ClientSession and deterministic policy gateway
|   |-- strategy, order-intent builder, executor, and reconciler
|   |-- pinned Alpaca MCP v2.3.0 child process over stdio
|   `-- audit writer
|-- private Streamlit operator UI (loopback only)
|   `-- full audit view and kill-switch controls
|-- public Streamlit judge UI (read-only volume, no credentials)
|   `-- sanitized evidence, health, decisions, orders, and equity
|-- Caddy HTTPS reverse proxy (public UI only)
`-- /data/thetatrap.sqlite3 (persistent volume, SQLite WAL)
```

Docker Compose starts the application roles with `restart: unless-stopped`; the worker uses `init: true` so its MCP child receives signals and is reaped cleanly. `alpaca-mcp-server==2.3.0` is installed and locked when the image is built, never downloaded at runtime. The worker owns one long-lived stdio MCP session and is the only process allowed to cross the policy gateway. SQLite uses WAL, a busy timeout, short transactions, and uniqueness constraints for strategy-day keys and client order IDs. A database-backed single-flight lease prevents a slow cycle or model call from overlapping the next 60-second tick.

Each Featherless request times out after 20 seconds. The pre-mutation agent loop has a 90-second budget, at most ten model turns, and at most twelve tool calls; this accommodates Qwen models that emit one native call per turn plus two bounded duplicate-read recoveries while retaining a hard cost ceiling. Only a failure before mutation dispatch may reject the candidate or advance the queue. MCP mutation calls receive at least 35 seconds, and any timeout after dispatch triggers client-ID reconciliation rather than `NO_TRADE`. Working intact-condor exits and reconciliation do not depend on the LLM; abnormal or terminal states remain visibly `RISK_OFF` for manual resolution. No Redis, Postgres, or queue is required.

If the MCP child or session fails, the worker blocks new entries, restarts the child, reinitializes the session, reruns `list_tools`, revalidates the stored schema hash, and reconciles account/order/position state before rearming. A schema mismatch is a hard startup failure, not an invitation to guess changed arguments.

The private Streamlit operator service listens inside its container while Compose publishes it only on `127.0.0.1:8501`; it is accessed through an SSH tunnel and owns the kill-switch controls. A separate public read-only judge dashboard is implemented behind HTTPS, has no mutation route or credentials, and reads the competition evidence through a read-only boundary. Alpaca and Featherless credentials are injected only into the worker role, and only the MCP child receives Alpaca keys.

## 11. Minimal interface

Both dashboards use the same credential-free operational report. The private operator page contains:

- Environment badges: `PAPER`, `BASIC INDICATIVE`, market status, account ID suffix, and strategy version.
- Worker heartbeat, Alpaca MCP version/session/schema-hash status, and last successful MCP/Featherless calls.
- Account equity, daily P&L, current risk, positions, and open orders.
- Current event and entry/exit countdown.
- Candidate legs, credit, maximum profit/loss, expected move, IV ratio, quote timestamps, and every policy gate.
- Qwen tool trace that distinguishes official MCP tools from local tools, the exact entry intent comparison, gateway decision, and explanation.
- Order/fill/exit status and realized P&L.
- Prominent `NO_TRADE` reason when applicable.
- Kill switch.
- Replay mode banner and fixture selector, visually distinct from competition mode.

The public judge page shows the paper/basic labels, build SHA, worker freshness, strategy outcome, candidate and gate evidence, bounded agent/tool trace, order/position status, equity, limitations, and kill-switch state. It hides emergency controls and raw identifiers, auto-refreshes, mounts evidence read-only, and receives no broker/model credentials.

Competition mode has no manual approval button. Replay mode may expose a simulation trigger only on the private surface and can never call order-mutating broker methods.

## 12. Persistence and audit

SQLite stores:

- Worker heartbeat and global app state.
- Immutable event definitions and strategy configuration hash.
- Market snapshots used for each candidate.
- Candidates and failed gate reason codes.
- Model name, tool trace, prompt/config hashes, structured response, and veto reason.
- Policy authorization.
- Client order ID, submitted order, broker response, status history, and fills.
- Position observations, exit attempts, account activities, and realized P&L.
- Timestamped account-equity snapshots.
- MCP server version, discovered-tool schema hash, session lifecycle, caller principal, tool arguments/results, and security metadata.
- Immutable pre-authorized order intents and exact-match gateway decisions.

Logs and the final report must clearly distinguish indicative quotes, simulated paper fills, replay results, and official competition results.

Replay uses a separate SQLite file and cannot modify competition strategy-day keys, kill-switch state, order state, equity history, or reports.

## 13. Reliability and security

- Secrets exist only in a Droplet environment file with restrictive permissions; never in Git, images, logs, model prompts, or the browser.
- The worker fails closed on missing configuration, broker/model errors, stale data, invalid JSON, or state disagreement.
- Startup always initializes the pinned MCP server, validates the tool-schema hash, and reconciles broker orders, positions, and activities before scanning.
- Model-visible MCP tools and arguments are locally allowlisted. News is restricted to the designated ticker and fixed lookback; account, clock, order, and position reads cannot carry arbitrary free-form scope.
- Bulk cancellation/liquidation, account-configuration mutations, exercise/do-not-exercise, crypto orders, and arbitrary stock orders are never exposed to Qwen.
- A worker heartbeat older than two minutes displays `WORKER STALE` and prevents new entries.
- Container logs are size-rotated to avoid filling disk.
- External event pages, news, model output, and attached documents are treated as untrusted data, not runtime instructions.
- Only paper endpoints are compiled/configured for this release.

An optional generic webhook may send critical worker, order, assignment, and exit-failure alerts after the core path is complete. It must not delay trading logic.

## 14. Acceptance criteria

The MVP is ready for competition only when all conditions pass:

1. Wrong account ID, non-paper mode, options level below 3, non-empty fresh account, or pre-start time prevents entry.
2. Missing, stale, crossed, or excessively wide quotes reject that candidate with a reason code; Basic indicative option quotes are not rejected merely for failing artificial cross-leg timestamp synchronization.
3. A malformed or adversarial model response cannot alter numeric fields or submit an order; arbitrary or near-match Qwen `place_option_order` arguments never reach the official MCP mutation tool.
4. Candidate maximum loss is independently recomputed and cannot exceed `$500`.
5. Normal entry and exit of an intact condor contain exactly four valid, matched legs in one MLeg order; assignment, exercise, corporate-action, or unmatched-leg evidence produces an idempotent, audited `RISK_OFF` state and never a new entry.
6. Repeated ticks, slow model calls, concurrent UI access, network timeouts, repricing, and worker restarts cannot create parallel or duplicate logical orders.
7. Restart recovery reconstructs the current state from Alpaca and SQLite and catches overdue exits.
8. Kill switch prevents new entries and invokes risk-off handling for an existing position.
9. The testing account remains stopped/disarmed and fixture/replay-only; the first competition-account live order is the controlled canary after every read-only release gate passes.
10. Replay mode demonstrates eligible, rejected, model-vetoed, duplicate-timeout, and exit scenarios without broker mutation.
11. Dashboard and final report reconcile to Alpaca order, position, and equity records.
12. The Droplet survives a container restart with state and worker heartbeat restored.
13. Replay writes are isolated from official state, and the UI container possesses no Alpaca or Featherless credentials.
14. Thursday EOD and Friday 09:30 ET equity exports reconcile to Alpaca and are included in the final run report.
15. The pinned MCP child initializes over stdio, the expected official v2 tool schemas match the committed hash, and an unexpected schema change blocks entry.
16. An exact pre-authorized Qwen `place_option_order` call passes the gateway for the controlled first competition-order canary; natural-language `ALLOW` without the tool call does not trade.
17. Repository instrumentation proves every Alpaca read and mutation uses MCP and no direct Alpaca SDK/REST path exists.
18. An ambiguous post-dispatch timeout is reconciled by deterministic client order ID and cannot produce a blind retry or false `NO_TRADE`.
19. Read-only shadow evidence reports the structurally executable candidates and exact failed gates at each sample without converting an uncalibrated no-fill estimate into a result claim.

## 15. Implementation checkpoints

Checkpoint 1 through Checkpoint 4 are implemented in code and covered by the pre-submission automated baseline. The testing account stays stopped/disarmed and fixture/replay-only. The first competition-account live order is the controlled canary after read-only gates. The public read-only dashboard is implemented; Checkpoint 5 remains incomplete until the cloud restart proof, official run, and final collateral are complete.

### Checkpoint 1 — Read-only foundation

Create configuration, pin and launch the official Alpaca MCP child, implement MCP discovery/schema verification and read calls, add the event file, SQLite state, structured logs, and dashboard health view. Prove the testing and competition account IDs cannot be confused and no direct broker path exists.

### Checkpoint 2 — Deterministic strategy

Implement expected move, chain filtering, candidate construction, liquidity gates, ranking, payoff/risk math, and replay fixtures. Freeze the configuration and tests before order access.

### Checkpoint 3 — Agent orchestration

Bridge the approved discovered MCP schemas into Featherless tool calling, add local candidate tools, event-risk rejection, immutable `OrderIntent` review, the exact-match policy gateway, strict argument validation, and model evaluation. Prove Qwen can call official MCP reads and that only its exact pre-authorized official `place_option_order` call can initiate entry.

### Checkpoint 4 — Paper execution lifecycle

Implement idempotent MCP MLeg entry, order reconciliation, repricing, cancellation, atomic MCP exit, abnormal-position detection and audited risk-off handling, kill switch, and a controlled first competition-order canary after read-only validation.

### Checkpoint 5 — Cloud and submission

Deploy both roles with Docker Compose and persistent storage, test restart recovery, run the Monday competition-account shadow cycle, record the demo, and generate the one-page system summary and final run report.

## 16. Submission narrative

> ThetaTrap is a bounded autonomous earnings-volatility agent built on Alpaca's official MCP server. Qwen calls official MCP account, market, and news tools, investigates verified events, and either rejects a candidate or invokes the exact pre-authorized `place_option_order` tool. A deterministic policy gateway constructs the one-contract iron condor, caps loss, rejects altered arguments, revalidates quotes, and alone authorizes dispatch. The cloud worker survives MCP and process restarts, reconciles broker state, exits automatically through MCP, and exposes the complete agent-to-tool-to-order audit trail.

Do not describe ThetaTrap as proven profitable, fully safe, real-time OPRA-driven, or ready for live capital.

## 17. External dependencies and references

- [Hackathon page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
- [Organizer Q&A](https://docs.google.com/document/d/13XWsMvW3mFm26xGlBLvdzzJ_eZQ33T4ZrP-vd9eat50/edit?tab=t.0)
- [Official Alpaca MCP server v2](https://github.com/alpacahq/alpaca-mcp-server)
- [Alpaca MCP server documentation](https://docs.alpaca.markets/us/v1.4.2/docs/alpaca-mcp-server)
- [Alpaca MCP multi-leg client serialization issue #97](https://github.com/alpacahq/alpaca-mcp-server/issues/97)
- [Alpaca multi-leg options](https://docs.alpaca.markets/us/docs/options-level-3-trading)
- [Alpaca options data](https://docs.alpaca.markets/us/docs/historical-option-data)
- [Alpaca paper-trading limitations](https://docs.alpaca.markets/us/docs/paper-trading)
- [BLS September 2026 release calendar](https://www.bls.gov/schedule/2026/09_sched_list.htm)
- [Featherless tool calling](https://featherless.ai/docs/tool-calling)
- [Qwen3-Coder-Next](https://featherless.ai/models/Qwen/Qwen3-Coder-Next)
- [Frozen event universe and source evidence](EVENT_SOURCES.md)
- [PANW earnings announcement](https://investors.paloaltonetworks.com/news-releases/news-release-details/palo-alto-networks-announce-fiscal-fourth-quarter-and-fiscal-7)
- [MDB earnings announcement](https://investors.mongodb.com/news-releases/news-release-details/mongodb-inc-announces-date-second-quarter-fiscal-2027-earnings)
- [CRDO earnings announcement](https://investors.credosemi.com/news-events/news/news-details/2026/Credo-Schedules-First-Quarter-Fiscal-Year-2027-Financial-Results-Conference-Call/default.aspx)
- [GTLB earnings announcement](https://ir.gitlab.com/news/news-details/2026/GitLab-To-Announce-Second-Quarter-Fiscal-2027-Financial-Results/default.aspx)
- [DELL earnings announcement](https://investors.delltechnologies.com/node/20901)
- [AVGO earnings announcement](https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announce-third-quarter-fiscal-year-2026-financial)
- [SNOW earnings announcement](https://www.snowflake.com/en/news/press-releases/snowflake-to-announce-financial-results-for-the-second-quarter-of-fiscal-2027/)
- [NTAP earnings announcement](https://investors.netapp.com/news/news-details/2026/NetApp-Hosts-First-Quarter-of-Fiscal-Year-2027-Financial-Results-Webcast/default.aspx)
- [AI earnings announcement](https://c3ai.gcs-web.com/news-releases/news-release-details/c3-ai-announce-financial-results-fiscal-first-quarter-2027)

Before submission, confirm the final public-repository requirement and exact cutoff in the official Discord/Q&A. Those administrative details must not be inferred from the trading window.
