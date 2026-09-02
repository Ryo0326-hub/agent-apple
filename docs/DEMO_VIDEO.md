# Demo video script and shot list

Target length: 3 minutes. Record at 1080p. Use the public read-only dashboard for any shareable URL; show the private operator dashboard only in a cropped local recording with no address bar or access details.

## Recording rules

- Show `PAPER` and `BASIC INDICATIVE` continuously when broker data or results are visible.
- Hide API keys, full account UUIDs, broker order IDs, Droplet IPs, environment files, and terminal history.
- Label replay material `SIMULATION — ZERO BROKER MUTATIONS` on screen.
- Do not manufacture a fill. If the official account remains unchanged, explain the no-trade or unfilled evidence.
- Capture the final result only after reconciling positions and open orders to flat.

## Script and shots

### 0:00–0:15 — Hook

**Narration:**

“A trading idea can fit in one sentence. A safe autonomous trading agent cannot. ThetaTrap turns an earnings-options idea into a bounded AI agent that can investigate, decide, and use Alpaca tools without controlling its own risk limits.”

**Shot:** Cover card, then the public dashboard header showing paper mode, data label, environment, and worker health.

### 0:15–0:38 — Origin and problem

**Narration:**

“I started from a YouTube video about collecting option premium. Earnings can make options expensive, but stale quotes, event surprises, four-leg execution, and ambiguous order responses make automation difficult. I wanted the AI to contribute real judgment without letting it invent the trade.”

**Shot:** Simple earnings-volatility illustration followed by the seven-eligible/two-excluded event table.

### 0:38–1:05 — Strategy

**Narration:**

“The primary profile measures earnings volatility, expected move, liquidity, and quote freshness, then builds one bounded iron condor. After 314 rejected evaluations on September first and second, I added a clearly labeled September-third canary: QQQ or SPY, one-dollar wings, at least twenty cents credit, at most eighty dollars risk, and a same-day exit. It is not an automatic fallback. Only one initial broker entry attempt is allowed for the date.”

**Shot:** Candidate card with four legs, credit, wing width, maximum loss, and gate table. Highlight DELL and NTAP exclusion reasons.

### 1:05–1:37 — Agent and MCP proof

**Narration:**

“Qwen runs through Featherless and receives approved tool schemas. It calls account, market, position, order, and news tools, then either issues a finite veto or copies the exact approved order into Alpaca's official MCP tool. It cannot change the symbol, strikes, price, quantity, or risk.”

**Shot:** Architecture graphic, then the bounded tool trace. Highlight official MCP calls and the final decision.

### 1:37–2:08 — Execution safety

**Narration:**

“Immediately before dispatch, the policy gateway refreshes broker state and quotes. A date- and account-bound one-shot authorization is consumed atomically with the order attempt. Deterministic client IDs prevent duplicates after timeouts. The long-running worker handles repricing, cancellation, restart reconciliation, and the complete exit—next morning for earnings or the same afternoon for the canary.”

**Shot:** Authorization state, exact-intent comparison, state transitions, and one timeout-reconciliation replay. Keep replay label visible.

### 2:08–2:32 — Result

Choose exactly one script after the official run.

**Filled-trade version:**

“During the official paper window, ThetaTrap completed [COUNT] entry-and-exit cycle(s). The account moved from one hundred thousand dollars to [FINAL EQUITY], an observed change of [CHANGE]. The dashboard and exported report connect the model decision, tool calls, order chain, positions, and equity.”

**No-filled-trade version:**

“During the official paper window, no entry filled. Rather than presenting a replay as a real result, ThetaTrap records the exact veto, policy, market-data, or fill reason and leaves the account outcome unchanged. The complete evidence is in the final report.”

**Shot:** Final equity, flat-position evidence, fill count, report digest, and deployed Git revision.

### 2:32–2:52 — Engineering evidence

**Narration:**

"The integrated release has 316 passing automated tests and five broker-isolated replay scenarios covering eligibility, stale data, an AI veto, timeout recovery, and exit. The agent runs continuously on DigitalOcean using Docker Compose and persistent SQLite state."

**Shot:** CI result, replay summary, container health, and repository tree.

### 2:52–3:00 — Close

**Narration:**

“ThetaTrap is paper-only and uses indicative data, so this is not proof of profitability. The next step is broader causal testing, better market data, and more realistic execution-cost modeling.”

**Shot:** Project name, repository URL, live read-only dashboard URL, and LabLab URL.

## Final recording checklist

- Narration and on-screen figures agree with the final report.
- No placeholder text remains.
- No private operator control is usable from a public URL.
- Replay and official-account footage are visually distinct.
- Captions are enabled and links are readable for at least three seconds.
- Export, upload, and test the video while signed out.
