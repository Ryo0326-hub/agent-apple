# DigitalOcean production runbook

This runbook deploys one competition-account worker, a private operator console,
and a public read-only evidence dashboard on an Ubuntu 24.04 Droplet. Broker
execution remains Alpaca paper-only.

## Production boundary

```text
Internet -> Caddy :443 -> public-ui -> competition SQLite volume (read-only)
SSH tunnel ---------> private ui -> competition SQLite volume (read/write controls)
                                  worker -> Alpaca MCP + Featherless
```

- Only `worker` receives Alpaca and Featherless credentials.
- `ui` remains bound to `127.0.0.1:8501` and is the only dashboard with
  kill-switch controls.
- `public-ui` has no mutation controls, receives no credentials, and mounts the
  database read-only. It shares no network with the worker; Caddy can reach only
  this service.
- The testing account, credentials, and database remain on the local
  machine. Never copy them to the Droplet.

## Competition clock

All times are Eastern and the worker remains on `America/New_York`:

| Date | Operator checkpoint |
| --- | --- |
| Monday, Aug 31 | Deploy disarmed; discover UUID; verify MCP schema; run replay, real read-only Qwen smoke, restart persistence, and competition preflight. Do not authorize an entry. |
| Tuesday, Sep 1 | Fresh preflight around 14:30; authorize Sep 1 only; start before 14:50; entry window 14:50-15:40; unresolved entry orders canceled by 15:45. |
| Wednesday, Sep 2 | Reconcile/exit from 09:45-09:53; authorize Sep 2 only after Alpaca confirms zero positions and zero open orders; repeat the afternoon entry window. |
| Thursday, Sep 3 | Reconcile/exit from 09:45-09:53; create no new authorization; confirm broker-flat state and retain Thursday EOD equity. |
| Friday, Sep 4 | Capture the 09:30 equity snapshot, export the final report, update every result placeholder, and submit by 10:15. |

The Tuesday and Wednesday authorizations are independent and sequential. There
may never be simultaneous positions. At the `$500` per-trade cap, the two-date
theoretical maximum loss is approximately `$1,000`; the `$99,000` equity stop
remains binding.

## 1. Provision the host

Create a Basic Ubuntu 24.04 Droplet in NYC3 with at least 2 GiB RAM. Use SSH-key
authentication, enable DigitalOcean monitoring and backups, and assign a
Reserved IP. For the time-limited submission, derive a no-registration hostname
from that address: replace dots with dashes and append `.sslip.io` (for example,
`192-0-2-10.sslip.io`). If that hostname does not resolve to the Reserved IP,
use the same dashed address under `.nip.io`. A custom domain can replace this
later but is not required for submission.

Apply a DigitalOcean Cloud Firewall with these inbound rules:

- TCP 22 from the operator's current IP/CIDR only.
- TCP 80 from all IPv4/IPv6 addresses for certificate issuance and redirect.
- TCP 443 and UDP 443 from all IPv4/IPv6 addresses for HTTPS/HTTP3.
- No rule for 8501 or any other application port.

Keep outbound TCP, UDP, and ICMP enabled so Docker, DNS, NTP, Alpaca, and
Featherless work. Install Docker Engine and the Compose plugin from Docker's
official Ubuntu repository, then enable Docker at boot.

Create a non-root operator and repository directory:

```bash
sudo adduser --disabled-password --gecos "" thetatrap
sudo usermod -aG docker thetatrap
sudo install -d -o thetatrap -g thetatrap -m 0700 /home/thetatrap/.ssh
sudo install -o thetatrap -g thetatrap -m 0600 \
  /root/.ssh/authorized_keys /home/thetatrap/.ssh/authorized_keys
sudo install -d -o thetatrap -g thetatrap -m 0750 /opt/thetatrap
sudo install -d -o thetatrap -g thetatrap -m 0700 /opt/thetatrap-backups
```

Open a second terminal and prove SSH access as `thetatrap` before changing any
root-login policy. Reconnect after adding the Docker group. Treat membership in
that group as root-equivalent host access.

## 2. Check out an immutable release

As `thetatrap`, clone the public repository and check out the exact submission
tag or commit. Do not deploy a moving branch:

```bash
git clone https://github.com/Ryo0326-hub/agent-apple.git /opt/thetatrap/app
cd /opt/thetatrap/app
git checkout --detach REPLACE_WITH_RELEASE_TAG_OR_SHA
git status --short
git rev-parse --verify HEAD
```

`git status --short` must be empty. Export the exact build identity, Reserved
IP, and derived public hostname in every deployment shell:

```bash
export THETATRAP_BUILD_SHA="$(git rev-parse --verify HEAD)"
export THETATRAP_RESERVED_IP="REPLACE_WITH_RESERVED_IP"
export THETATRAP_PUBLIC_HOST="${THETATRAP_RESERVED_IP//./-}.sslip.io"
getent ahostsv4 "${THETATRAP_PUBLIC_HOST}"
```

The build SHA is baked into the image label and displayed on both dashboards.
Replace the IP placeholder before running these commands and confirm the lookup
returns the Reserved IP. If `.sslip.io` does not resolve, set
`THETATRAP_PUBLIC_HOST` to the equivalent `.nip.io` hostname and repeat the
lookup. Do not start Caddy until the lookup succeeds.

## 3. Install competition secrets

Create the role file outside the Git checkout:

```bash
sudo install -d -o thetatrap -g thetatrap -m 0700 /etc/thetatrap
sudo install -o thetatrap -g thetatrap -m 0600 /dev/null \
  /etc/thetatrap/competition.env
sudo -u thetatrap nano /etc/thetatrap/competition.env
```

Use `.env.competition.example` as the field list. Insert only the fresh
competition account UUID and its keys. The UUID is Alpaca's account `id`, not
the `PA...` display number. Start with:

```dotenv
THETATRAP_ENVIRONMENT=competition
THETATRAP_READ_ONLY=true
THETATRAP_EXECUTION_ENABLED=false
ALPACA_PAPER_TRADE=true
```

Do not put `THETATRAP_BUILD_SHA` or `THETATRAP_PUBLIC_HOST` in this application
role file; export them in the deployment shell. Never print `docker compose
config`, because its resolved output contains secrets. `config --quiet` is safe.

Before uploading this file to the Droplet, discover the competition UUID once
on the trusted local machine while the placeholder UUID is still present:

```bash
PYTHONPATH=src .venv/bin/python -m thetatrap.cli \
  --env-file .env.competition discover-account
```

The command calls only MCP `get_account_info`, prints the full UUID locally,
and does not bind it to the database. Copy the returned
`THETATRAP_EXPECTED_ACCOUNT_ID=...` assignment into the protected competition
file. Do not put the UUID or command output in Git, screenshots, or public logs.

## 4. Build and start disarmed

All production commands use both Compose files and the same project name:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  config --quiet

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  build worker ui public-ui

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  run --rm --no-deps worker python -m thetatrap.cli check-config

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  run --rm --no-deps worker python -m thetatrap.cli agent-smoke

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  run --rm --no-deps worker python -m thetatrap.cli preflight
```

`agent-smoke` must complete all five read-only official MCP tools with zero
mutation tools exposed, then return a structured `READY` or `NOT_READY` result
with finite reason codes. Its `PASS` outcome means the bounded model/MCP loop
worked; account readiness is reported separately. Preflight must show the intended redacted
competition-account suffix, `ACTIVE`,
options level 3 or higher, `$100,000` initial equity, zero positions, zero open
orders, and the committed MCP schema hash. `EXECUTION_DISARMED`, `MARKET_CLOSED`,
and `ENTRY_AUTHORIZATION_MISSING` are expected baseline blockers.

Start the complete stack:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  up -d worker ui public-ui caddy

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  ps
```

Verify the public viewer and TLS without exposing the operator console:

```bash
curl --fail --silent --show-error \
  "https://${THETATRAP_PUBLIC_HOST}/_stcore/health"
```

Open the private console from the local machine through SSH:

```bash
ssh -N -L 8501:127.0.0.1:8501 thetatrap@DROPLET_RESERVED_IP
```

Browse to `http://127.0.0.1:8501`. The public URL must contain no emergency
buttons; the private URL must show them. Both must display the same build SHA.
Stopping or losing the worker must not stop `public-ui`; it should stay online
and display unavailable or stale evidence while the operator investigates.

## 5. Separate test and competition operation

Use the same frozen code revision but separate processes, keys, account UUIDs,
and databases:

1. Keep the testing account local and disarmed. Use it only for fixtures and
   broker-isolated replay; do not start a broker-connected worker for it.
2. Complete the full test suite, isolated replay, pinned MCP schema check, real
   Featherless tool-call probe, and competition-account read-only preflight.
3. Stop the local testing worker before competition execution so it cannot
   spend Featherless credit or create parallel evidence.
4. Treat the competition account's first date-bound paper entry as the one live
   canary. Arm it only after every read-only gate above passes, then keep that
   long-lived Droplet worker running through exit and broker-flat confirmation.

If the competition scan produces no eligible candidate, no order is forced and
the no-trade audit remains the correct result. Never weaken a gate to manufacture
a trade or P&L.

## 6. Arm one competition entry

Stop the worker before changing its environment. Do not stop it later while an
order or position may exist:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  stop worker
```

Edit `/etc/thetatrap/competition.env` so the flags are exact opposites:

```dotenv
THETATRAP_READ_ONLY=false
THETATRAP_EXECUTION_ENABLED=true
```

Run preflight in a one-off container before authorization. It performs broker
reads and may persist observations, but cannot place an entry without a durable
authorization:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  run --rm --no-deps worker python -m thetatrap.cli preflight
```

Replace the date and suffix below with the exact verified strategy date and
redacted suffix returned by preflight:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  run --rm --no-deps worker \
  python -m thetatrap.cli entry-authorization arm \
  --strategy-date 2026-09-02 \
  --reason "approved competition paper entry" \
  --confirm "ARM ONE PAPER ENTRY competition 2026-09-02 …ABC123"

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  run --rm --no-deps worker \
  python -m thetatrap.cli entry-authorization status \
  --strategy-date 2026-09-02
```

Then recreate the long-lived services so both dashboards show the armed flags:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  up -d --force-recreate worker ui public-ui caddy
```

Never run `worker --once` while execution is enabled. Keep the worker alive
through entry reconciliation, cancellation/repricing, the next-morning exit,
and final broker-flat confirmation.

## 7. Emergency operation

The public viewer only displays emergency state. Activate controls from the
SSH-tunneled operator UI or CLI. Do not stop a healthy worker merely because
the kill switch is on; it owns reconciliation and position-reducing exits.

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  exec -T worker python -m thetatrap.cli kill-switch on \
  --reason "operator emergency stop"
```

If the authorization is still armed and unconsumed, revoke it separately:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  exec -T worker python -m thetatrap.cli entry-authorization revoke \
  --strategy-date 2026-09-02 --reason "operator canceled competition entry"
```

The kill switch blocks new exposure and asks the worker to cancel/reduce intact
strategy exposure when the market permits. It is not an instant liquidation
guarantee. Assignment, exercise, a broken four-leg position, or a failed worker
requires manual action in the Alpaca paper dashboard. Clearing the kill switch
never approves a new entry.

## 8. Durable backup and restart proof

Create an online SQLite backup before an update and after the final flat state:

```bash
export THETATRAP_BACKUP_FILE="competition-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  exec -T worker python -c \
  "import sqlite3; source=sqlite3.connect('/data/thetatrap.sqlite3'); target=sqlite3.connect('/tmp/thetatrap-backup.sqlite3'); source.backup(target); target.close(); source.close()"

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  cp worker:/tmp/thetatrap-backup.sqlite3 \
  "/opt/thetatrap-backups/${THETATRAP_BACKUP_FILE}"

chmod 0600 "/opt/thetatrap-backups/${THETATRAP_BACKUP_FILE}"
sha256sum "/opt/thetatrap-backups/${THETATRAP_BACKUP_FILE}"
```

Restart the worker and prove state survived in the named volume:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  restart worker

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  exec -T worker python -m thetatrap.cli entry-authorization status
```

Never run `docker compose down -v`; `-v` deletes the competition audit state.

## 9. Safe update and evidence export

Update only while preflight proves zero positions and zero open orders. Back up
SQLite first, check out an explicit reviewed revision, export its new build SHA,
rebuild, rerun `config --quiet`, `check-config`, and preflight, then recreate the
stack. Do not update a worker that is managing an open position.

Export final evidence without a broker mutation:

```bash
docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  exec -T worker python -m thetatrap.cli report \
  --output /data/competition-final-report.md

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  cp worker:/data/competition-final-report.md \
  /opt/thetatrap-backups/competition-final-report.md

docker compose \
  --env-file /etc/thetatrap/competition.env \
  -p thetatrap-competition \
  -f compose.yaml -f compose.production.yaml \
  logs --no-color --since 24h worker \
  > /opt/thetatrap-backups/competition-worker.log
```

Inspect exported evidence before sharing it. Never publish environment files,
full account UUIDs, API keys, authorization IDs, raw Compose configuration, or
unbounded logs.
