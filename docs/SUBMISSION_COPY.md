# Exact submission copy

Copy only after completing every replacement in the pre-submit block. Do not submit square-bracket placeholders.

## Title

ThetaTrap — A Bounded MCP-Native AI Options Agent

## Short description

ThetaTrap uses Qwen and Alpaca’s official MCP server to evaluate earnings risk and execute defined-risk paper options trades inside deterministic safety limits.

## Long description (168 words including the result placeholder; final replacement must remain at most 180)

ThetaTrap is a bounded Alpaca paper-options agent inspired by a YouTube idea: earnings can make premium expensive, so a defined-risk structure may benefit when volatility falls. Autonomy adds harder problems: stale quotes, inconsistent four-leg data, drifting model output, and ambiguous order timeouts.

ThetaTrap therefore separates judgment from control. The frozen universe has seven eligible events and two explicit exclusions, DELL and NTAP. Featherless-hosted Qwen calls approved tools to inspect eligible opportunities and can veto risky event context. Deterministic Python builds one-contract iron condors, caps maximum loss at the lower of $500 or 0.5% of equity, and allows one broker entry attempt per strategy date after reviewing up to two candidates sequentially. Every Alpaca read and order uses the official MCP server. A persistent worker handles reconciliation, cancellation, and next-morning exit.

[PRE-SUBMISSION RESULT: insert filled-trade count, final equity, realized P&L, and final-report URL; if no trade filled, state that exactly.]

ThetaTrap uses Alpaca Basic indicative data and simulated paper fills. It does not demonstrate proven profitability or live-capital readiness.

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
