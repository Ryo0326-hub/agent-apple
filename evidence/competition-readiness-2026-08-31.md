# Competition account read-only readiness evidence

Captured on August 31, 2026 against the dedicated Alpaca paper competition account. Identifiers, credentials, raw broker payloads, and database files are intentionally excluded.

## Qwen and official MCP smoke

| Field | Sanitized result |
| --- | --- |
| Completed | `2026-08-31T16:49:57Z` |
| Outcome | `PASS` |
| Model | `Qwen/Qwen3-Coder-Next` |
| Model turns | `2` |
| Official MCP calls | `5` |
| Read tools | `get_account_config`, `get_account_info`, `get_all_positions`, `get_clock`, `get_orders` |
| Structured readiness | `READY` |
| Mutation tools exposed | `0` |
| Required schema hash | `0d08e617a03b38fe236951ab7bafa6ae07195c1c3efca1db5fae61bdecc7db72` |

The model received only summary/redacted tool results. The audit database stores hashes and tool names rather than full account or order identifiers.

## Deterministic competition preflight

| Field | Sanitized result |
| --- | --- |
| Observed | `2026-08-31T16:50:18Z` |
| Environment | `competition` |
| Broker mode | Paper |
| Account status | `ACTIVE` |
| Equity | `$100,000` |
| Options level | `3` |
| Open orders | `0` |
| Positions | `0` |
| Execution | Disarmed |
| Entry authorization | Missing, as required for deployment day |

No order was authorized, submitted, replaced, canceled, or filled during either check. This evidence establishes connectivity and release readiness only; it does not establish profitability.
