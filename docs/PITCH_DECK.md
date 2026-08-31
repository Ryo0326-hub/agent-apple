# Six-slide pitch deck content

Keep each slide visual and use speaker notes for detail. Target a three-minute pitch plus questions.

## Slide 1 — From a trading idea to a bounded agent

**Headline:** ThetaTrap — a bounded MCP-native AI options agent

**On-slide copy:**

- Earnings can make option premium expensive.
- A four-leg options idea is easy to describe and hard to automate safely.
- ThetaTrap gives AI real judgment while deterministic code owns risk.

**Visual:** Cover image combining an earnings-volatility curve, a four-leg payoff outline, and the Qwen → policy → Alpaca MCP flow.

**Speaker note:** Explain that the project began from the attached YouTube video, then shifted from “find a profitable trade” to “build an inspectable autonomous system.”

## Slide 2 — The strategy in plain English

**Headline:** Sell bounded earnings premium only when the evidence aligns

**On-slide copy:**

1. Freeze first-party-verified earnings events.
2. Check volatility term structure, expected move, liquidity, and freshness.
3. Build one symmetric, one-contract iron condor.
4. Let Qwen veto qualitative event risk.
5. Exit the complete position the next morning.

**Facts:** Seven eligible events; DELL excluded for ambiguous release time; NTAP excluded because required weekly expirations were unavailable. Up to two candidates may be reviewed sequentially, but only one broker entry attempt is allowed per date.

**Visual:** Four-leg payoff diagram beside the event table.

## Slide 3 — A real tool-using agent, not an AI label

**Headline:** Qwen decides; deterministic policy authorizes

**On-slide copy:**

- Featherless-hosted Qwen calls approved local and official Alpaca MCP tools.
- The model investigates account state, market data, orders, positions, and news.
- It returns a finite veto or the exact pre-authorized `place_option_order` call.
- It cannot alter symbol, strikes, expiration, quantity, price, or risk.

**Visual:** Architecture diagram with the AI decision path and deterministic execution path in different colors.

**Speaker note:** Emphasize that every Alpaca read and mutation uses the official MCP server; there is no direct SDK or REST fallback.

## Slide 4 — Failure-aware by design

**Headline:** The system assumes APIs, models, and processes can fail

**On-slide copy:**

- Maximum loss: lower of `$500` or `0.5%` of equity.
- One contract, one position, one initial broker entry attempt per date.
- Atomic one-shot authorization before dispatch.
- Deterministic client IDs and reconciliation after ambiguous timeouts.
- Restart-safe SQLite state and mandatory next-morning exit.
- Kill switch blocks new exposure but not position-reducing exits.

**Visual:** State timeline from candidate to flat, with timeout/restart branches.

## Slide 5 — Evidence and official outcome

**Headline:** Inspectable from model decision to account equity

**On-slide copy before the official run:**

- Integrated release: 181 automated tests passing.
- Five broker-isolated replay scenarios.
- Pinned Alpaca MCP package and required-tool schema hash.
- Public/private dashboard isolation and broker-isolated replay verified.
- **PRE-SUBMISSION:** add the first competition-order canary outcome, cloud restart proof, filled count, final equity, realized P&L, report digest, and deployed Git SHA.

**Visual:** Dashboard screenshot and a small evidence table. If no trade fills, show the exact no-trade/unfilled reason instead of a simulated result.

## Slide 6 — What this proves, and what comes next

**Headline:** Strong automation evidence; profitability remains an open research question

**On-slide copy:**

- Demonstrates bounded autonomy, MCP integration, explainability, and recovery.
- Uses Alpaca paper trading and Basic indicative data.
- One competition week cannot establish long-run expected value.
- Next: causal multi-event testing, OPRA-quality data, transaction-cost modeling, model-veto evaluation, and stronger monitoring.

**Visual:** Two-column “proven now / still to validate” summary plus repository and live-dashboard QR codes.

**Closing line:** ThetaTrap makes the agent's judgment useful, its authority narrow, and every outcome auditable.
