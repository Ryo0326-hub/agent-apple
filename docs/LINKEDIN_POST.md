# LinkedIn draft

The post body is intentionally kept between 220 and 300 words, excluding these instructions, links, and hashtags. Paraphrase it into the author's own voice and replace the marked result before publishing.

I started the Alpaca AI Trading Agents Hackathon with an idea from a YouTube video: earnings can make options expensive, so perhaps a defined-risk trade could collect that premium.

The strategy was easy to describe. Building an agent that could execute it safely was much harder.

ThetaTrap separates judgment from control. Featherless-hosted Qwen calls Alpaca tools to inspect account, market, order, position, and news evidence. It may veto a candidate or issue the exact approved order call. Deterministic Python chooses every leg and number, enforces one-contract risk, and manages reconciliation and exit. Every Alpaca interaction goes through the official MCP server.

The first two official days taught me something important. Across seven earnings symbols, the worker recorded 314 deterministic evaluations. Alpaca Basic's indicative data repeatedly had stale open-interest metadata or unusable quotes across one of the four required legs. No candidate passed every gate, so the system made zero broker attempts and stayed flat at $100,000.

I kept that result visible instead of pretending a replay was a real trade. I then made one transparent, date-scoped adaptation for September 3: an Intraday Theta Canary using QQQ or SPY, exact $1 wings, at least $0.20 credit, at most $80 defined loss, and a same-day exit. It is not an automatic fallback or a claim of proven alpha.

The final release includes a tested one-shot authorization, restart recovery, broker reconciliation, automatic exit, and separate private/public Streamlit dashboards. Judges can follow the original thesis, every failed gate, the pivot, Qwen's MCP trace, and any real order or P&L.

**Add the final September 3 broker-reconciled outcome here before publishing.**

This remains paper trading with non-consolidated indicative data. Next, I would test on OPRA-quality data, include realistic transaction costs, and evaluate the strategy and Qwen vetoes over many more market events.

Project: **PRE-SUBMISSION — add LabLab URL**

Code: https://github.com/Ryo0326-hub/agent-apple

#AIAgents #Fintech #OptionsTrading #MCP #Alpaca #OpenSourceAI
