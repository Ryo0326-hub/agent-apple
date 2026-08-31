# Release validation summary

Validated on August 31, 2026. This includes credentialed read-only checks against the dedicated competition paper account. No broker mutation or order submission occurred.

| Gate | Result |
| --- | --- |
| Automated suite | `191 passed` |
| Python compilation | Pass |
| Locked dependencies | `uv lock --check` passed; 104 packages resolved |
| Broker-isolated replay | `5 / 5` scenarios; zero external mutations |
| Production Compose | Configuration validation passed |
| Public container build | Pass on `python:3.12-slim` |
| Public root filesystem | Read-only write probe rejected |
| Public database mount | Read-only write probe rejected |
| Public credentials | Zero `ALPACA_*` or `FEATHERLESS_*` environment keys |
| Public/worker network separation | Public viewer attached only to the edge network |
| Desktop dashboard | 1440 by 900; no console warnings/errors or horizontal overflow |
| Mobile dashboard | 390 by 844; no console warnings/errors or horizontal overflow |
| Public control surface | No input/form controls, operator commands, credentials, or full UUIDs rendered |
| Refresh and freshness | 30-second evidence refresh configured; stale heartbeat warning rendered and verified |
| One-page PDF | One A4 page; visual and text checks passed |
| Pitch deck | Six slides; automated overflow check and per-page visual inspection passed |
| Qwen/MCP smoke | [`PASS`; 5 official reads; `READY`; zero mutation tools exposed](competition-readiness-2026-08-31.md) |
| Competition preflight | [ACTIVE; `$100,000` equity; options level 3; zero positions/orders; execution disarmed](competition-readiness-2026-08-31.md) |

Still required before live operation: deployed-image smoke/preflight, cloud restart proof, broker-reconciled paper result, and final URL/result replacement.
