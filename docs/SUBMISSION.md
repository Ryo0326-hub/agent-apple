# Competition submission and release checklist

Use this as the final operational checklist. Portal requirements, identifiers, and deadlines must be reconfirmed against the official [hackathon page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon), organizer Q&A, and Discord immediately before submission.

Internal deadline: finish the Friday, September 4 equity/report capture at 09:30 ET and submit by **10:15 ET**, preserving a 45-minute buffer before the current 11:00 ET portal deadline.

## What is being submitted

The product is a running DigitalOcean-hosted ThetaTrap worker, a public read-only judge dashboard, a private loopback operator dashboard, the public source repository, a demo video, pitch material, and the fresh Alpaca paper account whose order/equity history was produced by that worker.

The team—not the judges—runs the worker against the submitted paper-account credentials. Do not assume the `PA...` display number and Alpaca account UUID are interchangeable; enter the exact identifier requested by the portal.

## Fixed public facts

- Alpaca paper trading only.
- Alpaca Basic indicative options data and IEX underlying quotes.
- Seven eligible events: PANW, MDB, CRDO, GTLB, AVGO, SNOW, and AI.
- Two exclusions: DELL (`RELEASE_TIME_AMBIGUOUS`) and NTAP (`REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE`).
- Up to two ranked candidates may be reviewed sequentially before broker dispatch.
- One initial broker entry attempt is allowed per strategy date; at most one filled position.
- One contract; maximum loss is the lower of `$500` or `0.5%` of observed equity.
- No proven-profitability or live-capital claim.

## Account separation

- [ ] Testing account UUID, key pair, env file, and SQLite path differ from competition.
- [ ] Testing account remains stopped/disarmed and is used only for local fixtures and broker-isolated replay.
- [ ] The first competition-account live order is treated as the controlled canary after all read-only release gates pass.
- [ ] Competition account is fresh, starts at `$100,000`, has options level 3, is flat, and has no open orders.
- [ ] Competition credentials exist only in the protected competition env file and the credentialed worker.
- [ ] No second worker uses the competition account.
- [ ] The exact account identifier requested by LabLab/Alpaca is copied from the authoritative portal, not guessed.

## Software release gates

- [x] Integrated suite passes: 191 automated tests.
- [x] Five-scenario replay completes with zero external broker mutations.
- [x] Official `alpaca-mcp-server==2.3.0` and the committed required-tool schema hash are pinned.
- [x] Real Featherless Qwen probe completed five official MCP reads, exposed zero mutation tools, and returned structured `READY`.
- [ ] The first competition-account MLEG attempt is observed as the controlled live canary and remains under the one-shot lifecycle.
- [x] Testing and competition env-pair validation proves different account IDs, Alpaca key pairs, and database paths.
- [x] Competition read-only preflight verifies account suffix, ACTIVE status, level 3, `$100,000` equity, flat positions, zero open orders, clock, and MCP schema.
- [ ] One date/account/environment-bound entry authorization is recorded before startup and after consumption, revocation, or expiry.
- [ ] The armed service is the continuous worker; `worker --once` is not used while execution is enabled.
- [ ] Droplet worker survives a restart and reconciles the same persisted state.
- [ ] Final account is reconciled to zero positions and zero open orders.

## Repository release gates

- [ ] Secret scan finds no env file, key, secret, token, full account UUID, database, raw log, or Droplet access detail.
- [ ] `main` contains the reviewed source, lockfile, examples, MIT license, CI workflow, and documentation.
- [ ] GitHub Actions passes on the submitted revision.
- [ ] Repository About section has the project description, topics, public dashboard URL, and social-preview image.
- [ ] README links work while signed out.
- [ ] Deployed Git SHA is recorded in `evidence/deployed-revision.txt` and matches the Droplet checkout.
- [ ] Sanitized replay, validation, canary, and competition reports are committed or linked.
- [ ] Pre-hackathon/reused assets are disclosed accurately in [DISCLOSURES.md](DISCLOSURES.md).

## Dashboard and cloud gates

- [ ] Worker and private operator dashboard run on the competition Droplet with persistent SQLite state.
- [ ] Private operator dashboard remains loopback-only and is accessed through SSH.
- [ ] Public judge dashboard is status-only, has no mutation route, and uses a read-only data mount or snapshot.
- [ ] Public dashboard container has no Alpaca or Featherless credentials.
- [ ] Public page shows paper/basic-indicative labels, worker freshness, candidate/decision state, position state, and official account outcome.
- [ ] Public page auto-refreshes and shows a stale-data warning when the worker heartbeat ages out.
- [ ] HTTPS URL works in a signed-out browser on desktop and mobile.
- [ ] Kill-switch state may be visible publicly, but activation and clearing controls exist only in the private operator surface.
- [ ] Firewall exposes only SSH from the operator address plus HTTP/HTTPS for the public viewer; port 8501 is not public.
- [ ] Database volume backup and final-report export have been tested.

## Submission collateral

- [ ] Exact [title and descriptions](SUBMISSION_COPY.md) contain no placeholder.
- [ ] [LinkedIn post](LINKEDIN_POST.md) is paraphrased into the author's voice, published, and linked.
- [ ] Cover image and GitHub social preview are exported from the same visual system.
- [ ] Architecture, dashboard, tool-trace, risk-gate, and final-equity images pass the [asset publication rules](assets/README.md).
- [ ] [Three-minute demo](DEMO_VIDEO.md) is uploaded, captioned, publicly viewable, and tested while signed out.
- [ ] [Six-slide deck](PITCH_DECK.md) and [one-page write-up](ONE_PAGER.md) match the final report.
- [ ] LabLab project lists every enrolled team member and required technology/category tags.
- [ ] Repository, demo, dashboard, LinkedIn, and final-report URLs are placed in the correct portal fields.

## Official-result freeze

Do this only after the competition worker has reconciled the account.

- [ ] Export the final Markdown and JSON reports from the competition volume.
- [ ] Record starting equity, final equity, observed change, filled entry/exit count, flat position state, open-order count, timestamp, report digest, and deployed Git SHA.
- [ ] Compare those values with the Alpaca paper-account activity page.
- [ ] Replace every `PRE-SUBMISSION` field across the repository and collateral.
- [ ] If no order filled, state that explicitly and name the recorded veto, gate, market-data, or fill reason.
- [ ] If an order was submitted but not filled, do not describe it as a trade.
- [ ] Search the repository for `PRE-SUBMISSION`, `TBD`, placeholder brackets, and stale test counts.
- [ ] Run the complete test suite and link check one final time.

## Demo sequence

1. Show the paper/basic-indicative banner, environment, worker freshness, and pinned MCP session.
2. Explain the seven eligible and two excluded events.
3. Show the deterministic candidate, expected move, IV ratio, four legs, credit, and maximum loss.
4. Show Qwen calling approved official MCP and local tools, then vetoing or issuing the exact order call.
5. Show the one-shot authorization, exact-intent check, fresh policy recheck, and single entry order chain.
6. Show deterministic reconciliation, cancellation/repricing, and next-morning exit.
7. Show final flat positions, open orders, account equity, and report digest.
8. Show timeout and kill-switch behavior; label replay scenarios as simulation.

## Claims boundary

Approved claim:

> ThetaTrap is an autonomous, MCP-native paper-options agent whose LLM investigates event risk and makes an executable tool decision inside deterministic exposure limits.

Do not claim proven profitability, live-capital readiness, real-time consolidated OPRA quality, guaranteed fills, or a completed trade when only replay/unfilled evidence exists. The complete language guide is in [DISCLOSURES.md](DISCLOSURES.md).

## Archive after submission

- Sanitized final report and report digest.
- Relevant redacted worker-log window.
- Deployed Git revision and image digest.
- Final portal copy and URLs.
- Final video/deck/cover sources.
- Screenshot of submitted account identifier and submission confirmation, stored privately.

Never archive environment files, full credentials, raw `docker compose config`, SSH keys, or unredacted account data in the public repository.
