# Broker-isolated replay summary

> **SIMULATION ONLY - ZERO BROKER OR MCP MUTATIONS**

Executed on August 31, 2026 from the integrated release. The replay uses deterministic fixtures and a fresh local SQLite database; it is engineering evidence, not a profitability result.

| Scenario | Result | Evidence |
| --- | --- | --- |
| Eligible candidate | Pass | One-contract iron condor; simulated maximum loss `$255.00` |
| Deterministic rejection | Pass | Stale underlying quote rejected with `UNDERLYING_QUOTE_STALE` |
| Model veto | Pass | Simulated `TRADING_HALT` veto; no order authorization |
| Ambiguous timeout | Pass | One simulated dispatch, deterministic client ID recovery, zero duplicate submissions |
| Opposite-side exit | Pass | Four entry legs and four opposite exit legs reconcile to `FLAT` |

- Scenarios passed: `5 / 5`
- External broker mutation calls: `0`
- Result digest: `9b89e34a3d076589670066cbe3f62a604670dea44d7c055c5b1a78dfe833b21c`

The official competition result will be reported separately from the submitted Alpaca paper account after broker reconciliation.
