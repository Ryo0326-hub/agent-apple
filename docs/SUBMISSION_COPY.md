# Exact submission copy

Copy only after completing every replacement in the pre-submit block. Do not submit square-bracket placeholders.

## Title

ThetaTrap — A Bounded MCP-Native AI Options Agent

## Short description

ThetaTrap uses Qwen and Alpaca’s official MCP server to evaluate bounded options candidates and execute paper trades inside deterministic safety limits.

## Long description (final replacement must remain between 100 and 180 words)

ThetaTrap is a bounded Alpaca paper-options agent inspired by a YouTube idea about collecting expensive earnings premium with defined risk. Featherless-hosted Qwen calls approved Alpaca MCP tools and may veto a candidate or issue only the exact pre-authorized order. Deterministic Python chooses every leg and number, enforces one contract and one broker entry attempt, rechecks live evidence, reconciles timeouts, and controls the exit.

After 314 earnings evaluations on September 1–2 produced no valid four-leg candidate under Alpaca Basic indicative data, ThetaTrap added a transparent September 3-only canary: QQQ/SPY, September 4 expiry, exact $1 wings, minimum $0.20 credit, maximum $80 loss, and a same-day exit. It is not an automatic fallback or a proven edge.

[PRE-SUBMISSION RESULT: insert filled-trade count, final equity, realized P&L, and final-report URL; if no trade filled, state that exactly.]

The public dashboard shows the thesis, failed gates, pivot, Qwen tool trace, orders, fills, and equity. This is paper trading, not proof of profitability or live-capital readiness.

## Suggested tags

- AI Agents
- Fintech
- Trading
- MCP
- Open Source LLM
- Python
- Alpaca
- Qwen
- Streamlit
- DigitalOcean

## Links

- Repository: https://github.com/Ryo0326-hub/agent-apple
- Live read-only dashboard: https://104-236-77-186.sslip.io/
- Demo video: **PRE-SUBMISSION — add public URL**
- Final report: **PRE-SUBMISSION — add repository URL**
- LinkedIn post: **PRE-SUBMISSION — add post URL**

## Final-result replacement block

Replace the bracketed result sentence in the long description with one truthful sentence. Use the corresponding form below.

**If one or more trades fill:**

> During the official paper-trading window, ThetaTrap completed **[COUNT]** filled entry/exit cycle(s); the account moved from **$100,000.00** to **$[FINAL_EQUITY]**, an observed change of **$[CHANGE]**. Full timestamped evidence: **[REPORT_URL]**.

**If no trade fills:**

> During the official paper-trading window, ThetaTrap completed no filled entry. The account remained at **$[FINAL_EQUITY]**, and the report records each no-trade or unfilled reason: **[REPORT_URL]**.

Never change “paper” to “live,” describe an unfilled order as a trade, or present one competition result as proof of long-run profitability.
