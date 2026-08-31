# Public evidence

This directory is for sanitized, judge-readable evidence. It intentionally does not contain runtime databases, environment files, raw broker responses, or full worker logs.

## Add before final submission

- `replay-summary.md` — five scenarios, pass/fail state, zero external broker mutations, and report digest.
- `competition-canary.md` — the first competition-account paper entry-to-flat outcome, or an explicit statement that the gate did not complete.
- `competition-final-report.md` — sanitized report exported from the submitted competition account.
- `deployed-revision.txt` — exact Git commit SHA running on the Droplet.
- `validation-summary.md` — tests, MCP version/hash, restart proof, public-dashboard isolation, and timestamp.

## Redaction checklist

- No API keys, secrets, tokens, passwords, or environment files.
- No full Alpaca account UUID or unrelated account activity.
- No unredacted `docker compose config`, Droplet access details, or SSH material.
- No claim that replay results or paper fills prove profitability.
- Every artifact identifies `PAPER`, `BASIC INDICATIVE`, `REPLAY`, or `OFFICIAL COMPETITION` as applicable.
