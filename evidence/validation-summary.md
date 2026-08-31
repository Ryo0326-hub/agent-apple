# Release validation summary

Validated on August 31, 2026. This includes credentialed read-only checks against the dedicated competition paper account. No broker mutation or order submission occurred.

| Gate | Result |
| --- | --- |
| Deployed release | `v1.0.2-hackathon` at `f16a93de5df4e3bfd29b82be4cb3e3359c742c4b` |
| Public dashboard | `https://104-236-77-186.sslip.io/`; valid HTTPS and HTTP redirect |
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
| Cloud restart recovery | Same persistent database; WAL/index retained; zero authorizations and order attempts before/after restart |
| One-page PDF | One A4 page; visual and text checks passed |
| Pitch deck | Six slides; automated overflow check and per-page visual inspection passed |
| Qwen/MCP smoke | [`PASS`; 5 official reads; `READY`; zero mutation tools exposed](competition-readiness-2026-08-31.md) |
| Competition preflight | [ACTIVE; `$100,000` equity; options level 3; zero positions/orders; execution disarmed](competition-readiness-2026-08-31.md) |

Still required before live operation: Tuesday's fresh preflight and date-bound entry authorization. Still required before submission: a broker-reconciled paper result (including an honest no-fill/veto result if applicable), final report/collateral replacement, backup/export verification, and an operator-IP restriction for SSH if a stable source address is available.
