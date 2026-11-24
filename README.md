# zerogex-alerts

SPX day-trading level poller + ChartGT alert gate. Reads ZeroGEX dealer
levels, posts plain-English alerts to Discord. **No auto-trading** — the
user places every trade himself.

## How it works

- Poller fetches SPX 0DTE + all-chain levels, SPY all-chain levels, and
  the 0DTE position imbalance every 60 seconds during market hours
  (09:30–16:00 ET, weekdays), every 15 minutes outside hours.
- `GET /levels?symbol=SPX&expiration=0dte` returns the cached levels.
  Always use the **0DTE** scope for 0DTE trading — on Nov 23 close the
  SPX 0DTE call wall was 7275 vs 7400 all-chain.
- `POST /webhook/chartgt` gates a ChartGT scalper alert: a CALL only
  passes above the mood line (gamma flip) and not within 10 points of
  the 0DTE ceiling (call wall); a PUT only passes below the mood line
  and not within 10 points of the 0DTE floor (put wall). If the flip is
  missing, the SPY flip is used as a fallback, scaled to SPX terms
  (SPY is ~SPX/10, so 731.01 in SPY dollars is ~7310 in SPX dollars);
  if that is missing too, the flip check is skipped and logged. Stale
  levels (older than 10 minutes) return 503 and are never forwarded
  as passed.
- The flip can be null in the ZeroGEX API. This service never invents one.

## Discord wording

Every Discord message is plain English, phone-readable, under ~12 lines.
No jargon (no GEX, MSI, PCR, vanna, charm, delta, theta, gamma flip,
imbalance score, percentages or scores in Discord messages).

- Ceiling = call wall, "sellers likely push back here"
- Floor = put wall, "buyers likely step in here"
- Mood line = gamma flip, "above it buyers in control, below it sellers"
- "Price tends to drift toward" = max pain
- If the mood line is missing: "Mood line not clear today"

Morning alert:

```
SPX today - Tue Nov 24, 2026
Price now: $7,237
Ceiling to watch: $7,275
Floor to watch: $7,000
Price tends to drift toward: $7,355
Mood line: not clear today
What this means: Price is in the middle, no strong push either way. ...
Watch out for: Moves can get bigger and choppier today.
Levels from about 8 minutes old.
```

Pass / block alert:

```
PASSED - CALL at $7,250
Why: Price is above the mood line and still below the ceiling, so this fits.
Ceiling to watch: $7,275
Floor to watch: $7,000
Mood line: $7,240
```

## Alert schedule (user choice: "Morning + live alerts + close")

- **08:30 ET, weekdays:** morning levels alert (plain English)
- **Live, only when it matters:** a ChartGT webhook gate PASSED/BLOCKED,
  with one plain line why. (Wall hit/break alerts come from ZeroGEX
  itself, which already has a Discord webhook configured.)
- **14:30 ET, weekdays:** close alert — where price is vs the price it
  tends to drift toward, and what to watch into the close
- **No noon alert, no hourly ping, no overnight alerts.** About 3–5
  Discord messages a day max. The poller still checks every 60 seconds
  in session in the background; it just stays quiet unless one of the
  above triggers.

The 08:30 and 14:30 sends run **inside this service** (a scheduler
thread, checked every 30 seconds, one send per slot per day, inside a
5-minute window so a slow fetch does not lose the alert). No external
cron is needed on Coolify — as long as the app is running, the schedule
runs. If the app restarts, the sent-today marks are loaded back from
`data/state.json`, so a restart does not double-send.

Close alert:

```
SPX into the close - Tue Nov 24, 2026
Price now: $7,237
Price tends to drift toward: $7,355
Price is $118 below where it tends to drift toward.
Ceiling to watch: $7,275
Floor to watch: $7,000
Mood line: not clear today
What to watch: Watch for a slow move up into the close.
Levels from about 3 minutes old.
```

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/health` | `{"status":"ok", "last_fetch": ..., "symbol":"SPX"}` |
| GET | `/levels?symbol=SPX&expiration=0dte` | Cached levels (503 if no fresh data) |
| POST | `/webhook/chartgt` | Gate a ChartGT alert. Body: `{"symbol":"SPX","direction":"CALL"\|"PUT","price":7237,"time":"..."}` |
| POST | `/alert/morning` | Send the morning alert to Discord now |
| POST | `/alert/close` | Send the close alert to Discord now |
| POST | `/alert/test` | Send a test Discord message |

## Environment variables (set in Coolify, never in this repo)

| Variable | Where to get it |
| --- | --- |
| `ZEROGEX_API_KEY` | ZeroGEX → Account → API Access → Generate API Key (Pro plan) |
| `DISCORD_WEBHOOK_URL` | Discord → Channel Settings → Integrations → Webhooks → Copy Webhook URL |
| `CHARTGT_WEBHOOK_SECRET` | Optional. If set, `/webhook/chartgt` requires header `X-Webhook-Secret` with this value. Leave unset to keep the webhook open. |
| `PORT` | Default `8000` |

If `DISCORD_WEBHOOK_URL` is empty, the service still runs and returns
`200` with `"discord not configured"` — it never crashes on a missing
webhook. Never put a real key or webhook URL in code, in the Dockerfile,
or in git.

## Run locally

```bash
export ZEROGEX_API_KEY="..." DISCORD_WEBHOOK_URL="..."
python3 app.py
curl localhost:8000/health
curl -X POST localhost:8000/alert/test
```

## Deploy (Coolify)

Public repo, Dockerfile build pack, port 8000, domain
`https://zerogex-alerts.ssmedia.ca`. Set the two env vars above in the
Coolify dashboard (Application → Environment Variables), redeploy, then
test with `POST /alert/test` and check Discord.
