# LinkedIn draft

The post body is intentionally kept between 220 and 300 words, excluding these instructions, links, and hashtags. Paraphrase it into the author's own voice and replace the marked result before publishing.

I started the Alpaca AI Trading Agents Hackathon with an idea from a YouTube video. It made me curious about collecting option premium with defined risk around earnings.

The strategy sounded simple. Building an agent that could trade it safely was not.

I had to handle indicative option quotes, four-leg pricing, ambiguous order timeouts, model output that may drift, and a worker that must manage positions after the AI decision.

ThetaTrap separates those responsibilities. Featherless-hosted Qwen calls tools to inspect account, market, order, position, and news evidence. It can veto a candidate or issue the exact approved order call. Deterministic Python fixes the structure, strikes, price, and one-contract size, then caps maximum loss at the lower of $500 or 0.5% of equity. Every Alpaca interaction uses the official MCP server.

For the competition, I froze seven earnings events and excluded DELL for ambiguous timing and NTAP for unavailable weekly expirations. The agent may review two candidates, but gets only one broker entry attempt per date.

The release has 220 passing tests, five broker-isolated replay scenarios, restart recovery, one-shot authorization, automatic exit, and private/public Streamlit dashboards. Judges can see every scan, hard gate, Qwen review, MCP trace, order, fill, and equity update without exposing credentials.

On September 1, ThetaTrap recorded 176 evaluations across four symbols. None passed every hard gate, so it made zero broker attempts or fills and stayed flat at $100,000. I kept that no-trade result visible instead of changing the rules afterward. **Add the September 2 and final reconciled outcome before publishing.**

This is paper trading with Alpaca Basic indicative data, not proof of profitability. Next, I want to test the strategy over more events, model transaction costs more realistically, and compare Qwen's veto decisions across a larger dataset.

Project: **PRE-SUBMISSION — add LabLab URL**

Code: https://github.com/Ryo0326-hub/agent-apple

#AIAgents #Fintech #OptionsTrading #MCP #Alpaca #OpenSourceAI
