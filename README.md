# ThetaTrap

> A bounded, MCP-native AI agent for defined-risk Alpaca paper-options trading around verified earnings events.

[Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon) · [Product requirements](docs/PRD.md) · [Deployment runbook](docs/DEPLOYMENT.md) · [Submission package](docs/SUBMISSION.md)

![ThetaTrap cover](docs/assets/thetatrap-cover-1200x630.png)

ThetaTrap began with a simple idea from an options-trading video: earnings can make option premium expensive, and a defined-risk trade may benefit when that volatility falls after the announcement. The hard part was not describing the trade. It was building an agent that could inspect imperfect data, use a real broker interface, survive ambiguous failures, and still be unable to exceed fixed risk limits.

The result is a paper-trading agent in which Featherless-hosted Qwen investigates event risk and makes a genuine tool decision, while deterministic Python owns the candidate, strikes, price, size, maximum loss, and final permission to trade. Every Alpaca read and mutation crosses the pinned official Alpaca MCP server.

ThetaTrap is a hackathon engineering prototype. It uses Alpaca Basic indicative options data and simulated paper fills. It does **not** demonstrate proven profitability, live-capital safety, or real-time OPRA execution quality.

## At a glance

| Item | Current design |
| --- | --- |
| Trading environment | Alpaca paper trading only |
| Market data | Alpaca Basic indicative options data; IEX underlying quotes |
| Event universe | 7 eligible events; 2 deliberately excluded |
| Daily search | Up to 2 ranked candidates reviewed sequentially |
| Broker exposure | 1 initial broker entry attempt per strategy date; at most 1 filled position |
| Structure | 1-contract, four-leg iron condor |
| Maximum loss | Lower of `$500` or `0.5%` of observed equity |
| AI | `Qwen/Qwen3-Coder-Next`, with `Qwen/Qwen3-32B` fallback |
| Alpaca boundary | Official `alpaca-mcp-server==2.3.0` only |
| Verification baseline | 191 automated tests passing |
| Competition result | **PRE-SUBMISSION:** official fills, final equity, and realized P&L not yet recorded |

**PRE-SUBMISSION LINKS:** add the public read-only dashboard, demo video, final report, and LabLab project page after they are available.

## How the strategy works

1. **Start with verified events.** The schedule is frozen from first-party investor-relations announcements instead of trusting a live calendar scraper.
2. **Measure the volatility setup.** The worker compares front- and next-week at-the-money implied volatility, calculates the expected move, and checks quote freshness, spreads, open interest, and buying power.
3. **Construct one bounded candidate.** Deterministic code selects a symmetric iron condor and computes its exact four legs, credit, wing width, and maximum loss.
4. **Let the AI investigate.** Qwen calls approved account, market, order, position, and news tools. It may veto a candidate for a finite event-risk reason or issue the exact pre-authorized order call.
5. **Recheck before dispatch.** A policy gateway refreshes broker state and quotes, rejects any changed argument, consumes a date/account-bound one-shot authorization, and forwards at most one initial entry attempt for that strategy date.
6. **Manage the lifecycle.** Deterministic code reconciles ambiguous timeouts, reprices or cancels the same logical order chain, monitors positions, and submits the complete opposite-side exit the next morning.

An iron condor sells one put spread and one call spread around the expected earnings move. The received credit is the maximum possible profit; the wings define the maximum possible loss. ThetaTrap trades only one contract and never sells a naked option.

## What makes it an AI agent

Qwen is not decorative narration. It receives tool schemas, calls tools, examines evidence, and chooses whether an otherwise eligible candidate should proceed. Its decision can prevent a trade or produce the exact `place_option_order` call.

The model cannot choose the symbol universe, expiration, strikes, quantity, limit price, or risk. It cannot call replacement, cancellation, or exit mutations. This split gives the model a meaningful qualitative role without allowing probabilistic output to rewrite the trading policy.

```mermaid
flowchart LR
    W[60-second worker] --> S[Schedule and persisted state]
    S --> M[Official Alpaca MCP server]
    M --> D[Account, clock, news, quotes, chains]
    D --> G[Deterministic strategy and risk gates]
    G --> Q[Featherless Qwen tool loop]
    Q -->|veto| N[NO_TRADE]
    Q -->|exact tool call| P[Fresh policy recheck]
    P --> A[One-shot entry authorization]
    A --> M
    M --> R[Reconciliation, exit, and audit]
    R --> DB[(SQLite)]
    DB --> UI[Credential-free dashboard and report]
```

## Frozen competition universe

| Date | Eligible | Excluded |
| --- | --- | --- |
| September 1, 2026 | PANW, MDB, CRDO, GTLB | DELL — `RELEASE_TIME_AMBIGUOUS` |
| September 2, 2026 | AVGO, SNOW, AI | NTAP — `REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE` |

Up to two eligible candidates may be reviewed sequentially on a strategy date. A veto or deterministic failure before broker dispatch may advance to the next candidate. Once the single initial broker entry attempt is dispatched, ThetaTrap cannot start another candidate that date, even if the order receives no fill. Repricing remains part of that same logical order chain.

The first-party announcements and exclusion evidence are recorded in [docs/EVENT_SOURCES.md](docs/EVENT_SOURCES.md).

## Safety and reliability boundaries

- Paper trading is a configuration invariant; live-capital execution cannot be enabled.
- Exactly one contract and one open strategy position are allowed.
- The four-leg order must match an immutable, hashable intent exactly.
- Static environment flags are insufficient: one date-, account-, and environment-bound authorization must be consumed atomically before dispatch.
- Working orders use deterministic client IDs, preventing a timeout from creating an uncontrolled duplicate.
- A filled exit is not treated as flat until a later broker snapshot confirms zero positions and zero open orders.
- Assignment, exercise, corporate-action, non-option exposure, or broken-leg evidence enters audited `RISK_OFF` and requires manual broker resolution.
- The kill switch blocks new exposure but never blocks a position-reducing exit.
- The long-running worker owns reconciliation and exit; armed execution rejects `worker --once`.

## Implemented system

- Official Alpaca MCP v2.3.0 over stdio, with a pinned 19-tool required-schema hash and no direct Alpaca SDK or REST path.
- Native four-leg MLEG entry and exit, one logical replacement/cancel chain, cancel/fill race handling, and client-ID recovery after ambiguous mutation timeouts.
- SQLite audit records for tool calls, model decisions, market evidence, policy gates, authorizations, orders, fills, positions, equity, and state transitions.
- A single-flight worker lease and restart reconciliation.
- A private Streamlit operator dashboard, an isolated public read-only judge dashboard, and deterministic JSON/Markdown reports.
- A simulation-only replay covering an eligible candidate, stale-data rejection, model veto, duplicate-timeout recovery, and complete exit.
- Docker Compose deployment with a single worker and loopback-only operator UI.

## Evidence and current status

The integrated release passes **191 automated tests**. A credentialed competition-account smoke run completed all five official MCP reads through Qwen with zero mutation tools exposed, and the disarmed preflight confirmed an active, flat `$100,000` paper account with options level 3. The five-scenario replay makes zero external broker mutations. The testing account remains stopped/disarmed and is used only for local fixtures and replay. The first competition-account live order is itself the controlled canary after all read-only release gates pass.

Do not read a zero-trade replay or an unchanged account as profitability evidence. Final claims must come from the submitted competition account's timestamped equity and order history. See the sanitized [replay summary](evidence/replay-summary.md) and [release validation summary](evidence/validation-summary.md). Credentials, SQLite databases, and raw worker logs do not belong in public evidence.

## Quick start

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen --no-editable
PYTHONPATH=src .venv/bin/pytest -q
cp .env.dev.example .env.dev
chmod 600 .env.dev
```

Keep `.env.dev` disarmed. Placeholder credentials are sufficient for the broker-isolated baseline because it makes no network calls. Run:

```bash
PYTHONPATH=src .venv/bin/python -m thetatrap.cli --env-file .env.dev check-config
PYTHONPATH=src .venv/bin/python -m thetatrap.cli --env-file .env.dev init-db
PYTHONPATH=src .venv/bin/python -m thetatrap.cli \
  --env-file .env.dev replay --output-db artifacts/replay.sqlite3
```

View the fixture database locally without starting a broker-connected worker:

```bash
THETATRAP_DATABASE_PATH=data/dev/thetatrap.sqlite3 \
THETATRAP_ENVIRONMENT=development \
THETATRAP_READ_ONLY=true \
THETATRAP_EXECUTION_ENABLED=false \
PYTHONPATH=src .venv/bin/streamlit run src/thetatrap/dashboard.py
```

The operator dashboard binds to `127.0.0.1:8501`. It must remain private because it includes the kill switch. Never publish that service directly.

## Testing and competition accounts

The two accounts have deliberately different roles:

- **Testing account:** local fixture and replay only. Keep it stopped/disarmed in `.env.dev` with its own SQLite path; do not start a broker-connected worker for it.
- **Competition account:** the fresh `$100,000` account used from the beginning for the controlled live canary, cloud worker, and official submission. Keep it in `.env.competition` with a different UUID, key pair, and database volume. Arm its first order only after every read-only gate passes.

Never run two workers against one account. During the official window, the testing worker stays stopped or disarmed. Both roles run the same reviewed Git revision, but their credentials and state are never copied between environments.

The exact one-shot arming procedure, restart proof, emergency operations, and final report export are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Competition account discovery and AI smoke

Copy `.env.competition.example` to the ignored `.env.competition`, add the competition Alpaca key pair and Featherless key, and leave execution disarmed. Discover the UUID on the trusted local machine:

```bash
PYTHONPATH=src .venv/bin/python -m thetatrap.cli \
  --env-file .env.competition discover-account
```

This command calls only MCP `get_account_info`, prints the UUID locally, and does not bind it to the database. Put the returned UUID—not the `PA...` account number—into `THETATRAP_EXPECTED_ACCOUNT_ID`, then run:

```bash
PYTHONPATH=src .venv/bin/python -m thetatrap.cli \
  --env-file .env.competition agent-smoke
PYTHONPATH=src .venv/bin/python -m thetatrap.cli \
  --env-file .env.competition preflight
```

`agent-smoke` makes a real bounded Qwen request, exposes exactly five read-only official MCP tools, and requires a structured `READY` or `NOT_READY` result with finite reason codes. A `PASS` proves the MCP/model loop completed; the separate readiness field reports whether the observed account state is safe for the next preflight. It persists only redacted/hash evidence. Neither setup command has an entry-order tool path. Keep their raw local output out of the public repository.

## Repository map

```text
src/thetatrap/       agent, strategy, policy, MCP, execution, state, and UI
config/              pinned MCP contract and frozen event schedule
tests/               deterministic unit, integration, replay, and safety tests
docs/                PRD, deployment, submission, demo, deck, and disclosures
docs/assets/         publishable image and video asset manifest
evidence/            sanitized, judge-readable run evidence only
```

## Limitations

- Basic indicative options data is not consolidated OPRA data.
- Alpaca paper fills omit important live-market effects and cannot prove executable profitability.
- The event universe and expirations are intentionally frozen for one competition week.
- One official competition result cannot establish long-run expected value.
- Abnormal broken-position liquidation remains a manual broker operation.
- The system has not been approved for live capital.

## Future improvements

- Calibrate the premium signal against causally valid historical implied and realized volatility.
- Add OPRA-quality quotes and explicit transaction-cost/slippage modeling.
- Expand the event universe only after automated first-party event verification is reliable.
- Evaluate model decisions and veto quality across more events and open-source models.
- Extend the implemented public read-only judge dashboard with richer historical charts and alert context.
- Extend monitoring, alerting, and recovery testing beyond the competition window.

## Submission material

- [Exact portal copy](docs/SUBMISSION_COPY.md)
- [LinkedIn draft](docs/LINKEDIN_POST.md)
- [Demo video script and shot list](docs/DEMO_VIDEO.md)
- [Six-slide deck outline](docs/PITCH_DECK.md)
- [One-page project write-up](docs/ONE_PAGER.md)
- [Disclosures and claims](docs/DISCLOSURES.md)
- [Final checklist](docs/SUBMISSION.md)

## License and disclaimer

Released under the [MIT License](LICENSE).

This repository is for education, engineering evaluation, and paper trading. It is not financial advice, an offer to trade, or evidence that the strategy will be profitable.
