# LinkedIn final reflection draft

Paraphrase this into your own voice and use LinkedIn's mention picker to tag **lablab.ai** and **Alpaca** before publishing.

I just completed the trading portion of the Alpaca AI Trading Agents Hackathon with ThetaTrap, an autonomous paper-options agent built with Qwen and Alpaca's official MCP server.

My original idea came from a YouTube video about selling expensive option premium around earnings. The idea sounded simple, but reliable execution was the real challenge.

During the first two trading days, ThetaTrap evaluated 314 earnings candidates. None passed every safety and liquidity check, mainly because Alpaca Basic's indicative options data often lacked usable four-leg quotes or current open-interest information. The agent stayed flat instead of forcing a trade.

For the final day, I transparently adapted the system into a small intraday QQQ/SPY test. Qwen's first SPY attempt skipped a required evidence tool, so the deterministic gateway blocked it before Alpaca received an order. Qwen then completed all required calls for QQQ and submitted the exact approved four-leg trade through MCP.

The QQQ position opened for a $0.82 credit, closed automatically for a $0.95 debit, and was confirmed flat by 3:17 PM. My $100,000 paper account finished at $99,986.80: a $13.20 loss.

I did not prove a profitable strategy, but I did prove the full agent workflow: autonomous screening, real LLM tool use, bounded execution, and restart-safe reconciliation and exit.

Next, I would test with OPRA-quality data, longer backtests, realistic costs, and stronger model tool-sequencing evaluations.

Thank you to **lablab.ai** and **Alpaca** for the challenge.

Code: https://github.com/Ryo0326-hub/agent-apple

Demo: https://104-236-77-186.sslip.io/

#AIAgents #Fintech #OptionsTrading #MCP #Alpaca #BuildInPublic
