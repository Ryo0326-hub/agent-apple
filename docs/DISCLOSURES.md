# Disclosures and claims guide

This document is the source of truth for public claims in the LabLab entry, README, LinkedIn post, video, and deck.

## Trading and data disclosures

- ThetaTrap trades only in Alpaca paper accounts. No live-capital trading is supported or authorized.
- Options quotes come from Alpaca Basic indicative data; underlying quotes use IEX. This is not consolidated OPRA/SIP coverage.
- Alpaca paper fills are simulated and omit important live effects such as market impact, queue position, latency slippage, and some fees.
- The maximum-loss calculation describes the defined option structure. Assignment, exercise, corporate actions, and broken positions can create operational states requiring manual broker intervention.
- A paper-account equity change is evidence of that submitted run only. It does not prove a repeatable edge, positive expected value, or future profitability.
- A no-trade outcome is valid when mandatory evidence or policy gates fail. Do not replace missing data with adjacent bars or weaken a threshold after observing the desired result.
- The Sep 3 Intraday Theta Canary was designed after observing the September 1–2 feed constraints. It is a separately versioned, date-scoped competition adaptation, not backtest evidence and not a generic fallback after any earnings rejection.

## AI and autonomy disclosures

- Featherless hosts the open-source Qwen models used for orchestration.
- Qwen makes a real tool decision: it may veto an otherwise eligible candidate or issue the exact pre-authorized Alpaca MCP entry call.
- If every candidate fails deterministic gates, a separately labeled Qwen advisory may use six read-only Alpaca MCP tools to explain the best rejection. An advisory cannot construct an order, override a gate, change strategy state, or authorize execution.
- Deterministic code—not the model—selects the universe, structure, expiration, strikes, quantity, limit price, and risk.
- Repricing, cancellation, reconciliation, and exit are deterministic lifecycle operations.
- Every Alpaca read and mutation uses the pinned official Alpaca MCP server. There is no direct Alpaca SDK or REST fallback in application code.
- “Autonomous” means the long-running worker can perform the bounded paper lifecycle and recover persisted state. It does not mean unconstrained model authority or live-capital readiness.

## Strategy and universe disclosures

- The competition schedule contains seven eligible events and two explicit exclusions.
- DELL is excluded as `RELEASE_TIME_AMBIGUOUS`; its first-party announcement confirms a conference-call time but does not provide sufficient evidence for the exact results-release timing required by policy.
- NTAP is excluded as `REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE`; neither September 4 nor September 11 was available, and the nearest observed standard expiration was September 18.
- The agent may review up to two ranked candidates sequentially before broker dispatch. It permits one initial broker entry attempt per strategy date and at most one filled position.
- Replacement calls, if needed, remain within the same durable logical order chain; they are not a second strategy opportunity.
- With Basic indicative data, ThetaTrap searches outward from the expected-move thresholds when the nearest contract lacks usable liquidity metadata. Every alternative contract must remain outside the threshold and pass the same open-interest, quote, spread, credit, wing, and maximum-loss gates.
- The official September 1–2 earnings runs recorded 314 evaluations, zero eligible candidates, zero broker order attempts, and zero fills. This demonstrates fail-closed behavior under the observed data; it does not prove or disprove the strategy's expected value.
- The September 3 canary ranks QQQ and SPY by complete quote quality, uses September 4 options, one symmetric `$1`-wide condor, one contract, at least `$0.20` credit, and at most `$80` defined loss. It enters only from 09:45–10:45 ET and targets a same-day broker-flat state by 15:45.
- Canary open interest must remain numeric and at least `500` on each short and `100` on each wing. Its date may be up to three prior trading sessions old; quote freshness remains 10 seconds for the underlying and 60 seconds for options.

## Development and third-party disclosure

- Strategy brainstorming was inspired by the YouTube video [25 Years Of Brutal Options Trading Advice In 17 Minutes](https://www.youtube.com/watch?v=PM1JE3QbsjA). The video is inspiration, not backtest evidence or an endorsement of ThetaTrap.
- The product depends on the official Alpaca MCP server, Alpaca paper trading and market-data services, Featherless inference, Qwen models, Python open-source libraries, Streamlit, Docker, and DigitalOcean.
- **PRE-SUBMISSION:** state exactly whether any code, templates, infrastructure, prompts, or other assets existed before the hackathon. If so, list each item and distinguish it from work completed during the event.
- **PRE-SUBMISSION:** confirm that every listed team member is enrolled and that the repository visibility and reused-work disclosures meet the final organizer rules.

## Approved language

> ThetaTrap is an autonomous, MCP-native paper-options agent whose LLM investigates event risk and makes an executable tool decision inside deterministic exposure limits.

> After two official no-trade days exposed limitations in the Basic indicative feed, ThetaTrap added a transparently labeled September 3-only ETF canary with tighter `$80` defined loss and a same-day exit. This was an execution experiment, not a claim of proven alpha.

> During the official paper-trading window, the submitted account produced [OBSERVED RESULT]. This is a single paper result, not proof of future profitability.

## Prohibited or misleading language

Do not describe ThetaTrap as:

- proven profitable;
- guaranteed to trade, fill, or make money;
- ready or approved for live capital;
- driven by real-time consolidated OPRA data;
- safe from every assignment or execution risk;
- having completed an official trade when only a replay, submitted order, or unfilled order exists.
- portraying the Sep 3 canary as a prevalidated profitable strategy or an always-on production fallback.

## Final consistency check

The README, portal copy, LinkedIn post, video, deck, dashboard, final report, and Alpaca account history must agree on:

- eligible/excluded event counts;
- order and fill counts;
- starting and final equity;
- realized P&L wording;
- paper/basic-indicative labels;
- deployed Git revision;
- whether each displayed scenario is replay or official competition evidence.
