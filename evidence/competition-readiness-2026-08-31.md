# Competition account read-only readiness evidence

Captured on August 31, 2026 from the deployed DigitalOcean competition image against the dedicated Alpaca paper competition account. Identifiers, credentials, raw broker payloads, and database files are intentionally excluded.

## Deployed release

| Field | Sanitized result |
| --- | --- |
| Release | `v1.0.3-hackathon` |
| Git commit | `29edcb8ffe4f774d02e4e366f8c4a6372f06bf09` |
| Public dashboard | `https://104-236-77-186.sslip.io/` |
| HTTPS | Valid certificate; HTTP redirects to HTTPS |
| Worker mode | Long-running, paper-only, execution disarmed |

## Qwen and official MCP smoke

| Field | Sanitized result |
| --- | --- |
| Completed | `2026-08-31T17:46:51Z` |
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
| Observed | `2026-08-31T17:46:14Z` |
| Environment | `competition` |
| Broker mode | Paper |
| Account status | `ACTIVE` |
| Equity | `$100,000` |
| Options level | `3` |
| Open orders | `0` |
| Positions | `0` |
| Execution | Disarmed |
| Entry authorization | Missing, as required for deployment day |

## Cloud isolation and recovery

- The public container has zero Alpaca or Featherless environment keys, a read-only database mount, a read-only root filesystem, and access only to the edge network.
- The private operator dashboard binds only to server loopback and exposes the kill-switch control through an SSH tunnel.
- Desktop (`1440x900`) and mobile (`390x844`) browser checks rendered live evidence without horizontal overflow; the public view contained no form or input controls.
- Restarting the worker preserved the same SQLite inode and persistent WAL state. Entry-authorization, order-attempt, and order-event counts remained zero; the post-restart broker preflight confirmed zero open positions and zero open orders.

No order was authorized, submitted, replaced, canceled, or filled during these checks. This evidence establishes connectivity and release readiness only; it does not establish profitability.
