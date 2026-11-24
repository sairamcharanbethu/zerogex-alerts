# zerogex-alerts

SPX day-trading level poller + ChartGT alert gate. Reads ZeroGEX dealer
levels, posts plain-English alerts to Discord. **No auto-trading** — the
user places every trade himself.

## How it works

- Poller fetches SPX 0DTE + all-chain levels, SPY all-chain levels, and
  the 0DTE position imbalance every 60 seconds during market hours
  (09:30–16:00 ET, weekdays, excluding US market holidays), every 15
  minutes outside hours. The four fetches run **in parallel** (threads),
  so a poll cycle costs roughly one fetch (~1–2s), not the sum of four.
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
  levels (fetched over 10 minutes ago, or API data itself over 10
  minutes old) return 503 and are never forwarded as passed.
- The flip can be null in the ZeroGEX API. This service never invents one.

## Discord wording

Every Discord message is plain English, phone-readable, short format
(5 lines morning/close, 3 lines live). No jargon (no GEX, MSI, PCR,
vanna, charm, delta, theta, gamma flip, imbalance score, percentages
or scores in Discord messages). "Buys/sells" was replaced with
**calls/puts** on 2026-11-24 because "don't chase buys" was ambiguous:
calls = betting price goes up, puts = betting price goes down.
"Don't chase calls" means don't buy calls right now — it does NOT mean
buy puts instead. It means wait.

- Ceiling = call wall, "sellers likely push back here"
- Floor = put wall, "buyers likely step in here"
- Mood line = gamma flip, "above it buyers in control, below it sellers"
- "Price tends to drift toward" = max pain

Morning alert:

```
SPX today - Tue Nov 24, 2026 - WAIT — Don't chase calls
Price $7,237
Ceiling $7,275 (38 away) | Floor $7,000 (237 away)
Drift toward: $7,355 | Mood line: not clear today
What changes this: If price breaks above $7,275 and stays there, calls. If it turns back down, wait.
```

Live level alert (ZeroGEX only, no ChartGT needed):

```
CEILING HIT — SPX $7,275
Price just reached the ceiling.
Don't chase calls here, wait for a pullback or a break above and hold.
```

Gate alert:

```
PASSED - CALL $7,250, TRY IT
Why: Price is above the mood line and still below the ceiling, so this fits.
Try the call, 1 contract only. Get out if it turns back below the mood line.
```

## Alert schedule (user choice: "Morning + live alerts + close")

- **08:30 ET, weekdays (not market holidays):** morning levels alert.
  It only sends if the 0DTE levels are for **today's** expiry — if they
  are yesterday's, it fetches fresh once and retries inside the
  5-minute window; if still stale, it skips rather than sending
  yesterday's levels as if they were fresh.
- **Live, only when it matters (ZeroGEX only):** the poller watches
  price against the levels every ~60s in session and alerts once per
  level per day on: ceiling touch / break, floor touch / break,
  mood-line cross, drift level reached. A break needs a real crossing
  (previous poll on the other side) — a gap-up open above the ceiling
  fires a touch, not a false break. If several levels cross in one
  poll gap, all of them are sent (max 3), none is silently dropped.
- **Live, ChartGT gate:** a ChartGT webhook PASSED/BLOCKED, with one
  plain line why. (ChartGT is not connected yet — the webhook is ready
  at `/webhook/chartgt`.)
- **14:30 ET, weekdays (not market holidays):** close alert, same
  5-line format. It also requires fresh levels (API age under an hour).
- **No noon alert, no hourly ping, no overnight alerts.** About 3–5
  Discord messages a day max. The poller still checks every 60 seconds
  in session in the background; it just stays quiet unless one of the
  above triggers.

The 08:30 and 14:30 sends run **inside this service** (a scheduler
thread, checked every 30 seconds, one send per slot per day, inside a
5-minute window so a slow fetch does not lose the alert). No external
cron is needed on Coolify — as long as the app is running, the schedule
runs. If the app restarts, the sent-today marks are loaded back from
`data/state.json`, so a restart does not double-send. State saves are
locked and snapshotted, so concurrent saves from the poller, scheduler
and gate threads cannot corrupt the file.

Known limits (not bugs to "fix" by guessing):

- Polling samples price about once a minute, so a touch that lasts
  under a minute can be missed. There is no tick feed in the ZeroGEX API.
- Early-close days (1pm ET, e.g. Fri Nov 27 2026, Dec 24, Dec 31) are
  not special-cased: the 14:30 close alert still fires after the 1pm
  close. Live alerts stay quiet then because the API age grows past
  the 10-minute freshness cap.
- These are level alerts, not entry signals. ZeroGEX's own signals run
  ~50% on a 60-minute horizon, so the user still times the actual
  trade on his chart. A scalper cannot be built from ZeroGEX alone:
  Pine cannot call the API, and ZeroGEX gives gamma levels (OI-based,
  mostly static intraday), not 1-minute price bars or EMA/VWAP/RSI.

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/health` | `{"status":"ok", "last_fetch": ..., "symbol":"SPX"}` |
| GET | `/levels?symbol=SPX&expiration=0dte` | Cached levels (503 if no fresh data) |
| POST | `/webhook/chartgt` | Gate a ChartGT alert. Body: `{"symbol":"SPX","direction":"CALL"\|"PUT","price":7237,"time":"..."}` |
| POST | `/alert/morning` | Send the morning alert to Discord now |
| POST | `/alert/close` | Send the close alert to Discord now |
| POST | `/alert/test` | Send a test Discord message |
| POST | `/alert/live-test` | Send a sample live level alert (does not arm the watcher). Body: `{"event":"ceiling_touch"}` (also `ceiling_break`, `floor_touch`, `floor_break`, `mood_cross_up`, `mood_cross_down`, `drift_reached`) |

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
