# ThetaTrap

**A bounded, MCP-native AI agent for defined-risk Alpaca paper-options trading around verified earnings events**

## Problem and idea

ThetaTrap began with a YouTube idea: earnings can make option premium expensive, so a defined-risk trade may benefit when volatility falls afterward. Automating that idea is harder. Quotes may be indicative or stale, four legs must be priced consistently, model output can drift, order timeouts are ambiguous, and a restart cannot leave a position unmanaged.

## How it works

The worker starts from a frozen, first-party-sourced schedule. Seven events are eligible: PANW, MDB, CRDO, GTLB, AVGO, SNOW, and AI. DELL is excluded because its release time is ambiguous. NTAP is excluded because neither required weekly expiration is available.

For each strategy date, deterministic code retrieves Alpaca Basic indicative option chains and IEX quotes through the official Alpaca MCP server. It measures the volatility term structure and expected move, checks freshness and liquidity, and builds a symmetric one-contract iron condor. Maximum loss cannot exceed the lower of `$500` or `0.5%` of equity.

Up to two ranked candidates may be reviewed sequentially before broker dispatch. Featherless-hosted Qwen calls approved account, market, order, position, and news tools. It either vetoes a candidate for a finite event-risk reason or issues the exact pre-authorized order call. Once one initial broker entry attempt is dispatched, no second candidate can reach the broker that date.

## Judgment with bounded authority

Qwen cannot change the universe, expiration, strikes, quantity, price, or risk. A deterministic policy gateway refreshes broker state and quotes, compares every argument with the immutable intent, and consumes a date/account/environment-bound authorization before forwarding the MCP mutation.

Repricing, cancellation, timeout reconciliation, restart recovery, and next-morning exit remain deterministic. A reported exit fill becomes `FLAT` only after a later broker snapshot confirms no positions and no open orders. The private dashboard owns controls; the public dashboard is read-only and credential-free.

## Evidence and result

- Integrated release: 190 automated tests passing.
- Five simulation-only replay scenarios with zero external broker mutations.
- Pinned `alpaca-mcp-server==2.3.0` and required-tool schema hash.
- Persistent audit linking evidence, model decisions, policy, authorizations, orders, positions, and equity.

**PRE-SUBMISSION RESULT:** add the first competition-order canary outcome, filled count, starting/final equity, realized P&L, report digest/URL, and deployed Git revision. If no order fills, say so and preserve the recorded reason.

## Limitations and next steps

ThetaTrap is paper-only. Indicative quotes are not consolidated OPRA data, paper fills do not reproduce live execution, and one competition week cannot prove profitability. Next steps are broader causal event testing, OPRA-quality data, transaction-cost modeling, evaluation of Qwen's vetoes, and longer operational monitoring.

Repository: https://github.com/Ryo0326-hub/agent-apple

Dashboard: **PRE-SUBMISSION — add HTTPS URL** · Demo: **PRE-SUBMISSION — add URL**
