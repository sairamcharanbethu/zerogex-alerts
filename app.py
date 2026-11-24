#!/usr/bin/env python3
"""zerogex-alerts: SPX day-trading level poller + ChartGT alert gate.

Stdlib-only Python (no pip dependencies) so the ARM64 Coolify build is
trivial and fast. Fetches ZeroGEX dealer levels via curl (Cloudflare
blocks Python urllib's default User-Agent on api.zerogex.io) and posts
plain-English alerts to Discord.

Discord wording rule (user requirement, 2026-11-24):
  Every Discord message must be plain English, no unexplained jargon.
  No GEX / MSI / PCR / vanna / charm / delta / theta / gamma flip /
  imbalance score, no percentages, no scores in Discord messages.
  Call wall = "Ceiling - sellers likely push back here"
  Put wall  = "Floor - buyers likely step in here"
  Flip      = "Mood line - above it buyers in control, below it sellers"
  Max pain  = "Price the market tends to drift toward by end of day"
  Net GEX negative = "Moves can get bigger and choppier today"
  Net GEX positive = "Moves tend to stay calmer today"
  Keep every Discord message under ~12 lines, phone-readable.

No auto-trading: this service only reads levels and posts alerts.
The user places every trade himself in Wealthsimple.

Alert schedule (user choice, 2026-11-24 - "Morning + live alerts + close"):
  - 08:30 ET weekdays: morning levels alert (plain English)
  - Live, only when it matters: price hits/breaks ceiling/floor, or a
    ChartGT webhook gate PASSED/BLOCKED - one plain line why
  - 14:30 ET weekdays: close alert - where price is vs the price it
    tends to drift toward, what to watch into the close
  - NO noon alert, NO hourly ping, NO overnight alerts. About 3-5
    Discord messages a day max. The poller still checks every 60 sec
    in session in the background; it just stays quiet unless one of
    the above triggers. The 08:30 and 14:30 sends run inside this
    service (scheduler thread), no external cron needed.

Endpoints:
  GET  /health               -> status, last_fetch, config flags
  GET  /                     -> endpoint list (plain text)
  GET  /levels?symbol=SPX&expiration=0dte|all
                             -> cached latest levels (503 if no fresh data)
  POST /webhook/chartgt      -> ChartGT scalper alert gate (PASSED/BLOCKED)
  POST /alert/morning        -> send the morning levels alert now
  POST /alert/close          -> send the close alert now
  POST /alert/test           -> send a test Discord message
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - very old Python
    ET = timezone(timedelta(hours=-5))

ZEROGEX_BASE = "https://api.zerogex.io"
PLACEHOLDERS = {"", "FILL_IN_COOLIFY_DASHBOARD", "PLACEHOLDER", "CHANGE_ME"}
PORT = int(os.environ.get("PORT", "8000"))
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json"))
WALL_BUFFER = 10.0          # pts: block a trade within 10 pts of ceiling/floor
FRESH_SECONDS = 600         # gate needs levels fetched within 10 minutes
IN_SESSION_POLL = 60        # seconds, 09:30-16:00 ET weekdays
OUT_SESSION_POLL = 900      # seconds, otherwise (pre-open context only)


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _clean_env(name):
    v = (os.environ.get(name) or "").strip()
    return "" if v in PLACEHOLDERS else v


def zerogex_key():
    return _clean_env("ZEROGEX_API_KEY")


def discord_webhook():
    return _clean_env("DISCORD_WEBHOOK_URL")


def webhook_secret():
    return _clean_env("CHARTGT_WEBHOOK_SECRET")


# ---------------------------------------------------------------- state
state = {
    "spx_0dte": None,
    "spx_all": None,
    "spy_all": None,
    "imbalance": None,
    "last_fetch": None,
    "last_fetch_ok": False,
    "last_error": None,
    "poll_count": 0,
    "decisions": [],  # last 20 gate decisions, newest first
    "sent_slots": {},  # e.g. {"2026-11-24 morning": true} - one send per slot per day
}
_state_lock = threading.Lock()


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # state file is best-effort; memory is primary
        log(f"state save failed (non-fatal): {exc}")


def load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        with _state_lock:
            for key in ("spx_0dte", "spx_all", "spy_all", "imbalance",
                        "last_fetch", "last_fetch_ok", "last_error", "poll_count",
                        "sent_slots", "decisions"):
                if key in data:
                    state[key] = data[key]
        log("loaded cached state from disk")
    except Exception:
        pass


# ---------------------------------------------------------------- ZeroGEX
def _curl_json(url, headers):
    """GET url with curl (Cloudflare blocks Python urllib here)."""
    cmd = ["curl", "-s", "-m", "30", "-w", "\n%{http_code}"]
    for key, val in headers.items():
        cmd += ["-H", f"{key}: {val}"]
    cmd += ["-H", "User-Agent: curl/8.5.0", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {proc.stderr.strip()[:300]}")
    out = proc.stdout
    lines = out.rsplit("\n", 1)
    if len(lines) != 2 or not lines[1].isdigit():
        raise RuntimeError("could not read HTTP status from ZeroGEX response")
    status, text = int(lines[1]), lines[0]
    if status >= 400:
        raise RuntimeError(f"ZeroGEX HTTP {status}: {text[:500]}")
    return json.loads(text) if text.strip() else {}


def _normalize_levels(raw, symbol, expiration_label):
    """Flatten a v2 /levels response. Flip may be null - never invent one."""
    data = raw.get("data") or raw
    levels = data.get("levels") or {}
    profile = data.get("profile") or []
    profile_sum = None
    if profile:
        try:
            profile_sum = sum(float(p.get("net_gex") or 0) for p in profile)
        except Exception:
            profile_sum = None
    fetched_at = datetime.now(timezone.utc).isoformat()
    return {
        "symbol": symbol,
        "expiration": expiration_label,
        "expiration_date": data.get("expiration"),
        "spot": data.get("spot"),
        "as_of": data.get("as_of"),
        "age_seconds": data.get("age_seconds"),
        "fetched_at": fetched_at,
        "gamma_flip": levels.get("gamma_flip"),
        "gamma_flip_reason": levels.get("gamma_flip_reason"),
        "gamma_flip_label": levels.get("gamma_flip_label"),
        "call_wall": levels.get("call_wall"),
        "put_wall": levels.get("put_wall"),
        "max_pain": levels.get("max_pain"),
        "pin_strike": levels.get("pin_strike"),
        "net_gex_at_spot": data.get("net_gex_at_spot"),
        "net_gex_profile_sum": profile_sum,
    }


def fetch_levels(symbol, expiration):
    key = zerogex_key()
    if not key:
        raise RuntimeError("zerogex not configured (ZEROGEX_API_KEY missing)")
    url = f"{ZEROGEX_BASE}/api/v2/levels/{symbol}?strikes=40&expiration={expiration}"
    raw = _curl_json(url, {"Authorization": f"Bearer {key}", "Accept": "application/json"})
    return _normalize_levels(raw, symbol, expiration)


def fetch_imbalance(symbol="SPX"):
    key = zerogex_key()
    if not key:
        raise RuntimeError("zerogex not configured (ZEROGEX_API_KEY missing)")
    url = f"{ZEROGEX_BASE}/api/signals/advanced/0dte-position-imbalance?symbol={symbol}"
    raw = _curl_json(url, {"Authorization": f"Bearer {key}", "Accept": "application/json"})
    ctx = raw.get("context_values") or {}
    return {
        "symbol": symbol,
        "timestamp": raw.get("timestamp"),
        "score": raw.get("score"),
        "direction": raw.get("direction"),
        "signal": ctx.get("signal"),
        "triggered": ctx.get("triggered"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def poll_once():
    """One poll cycle: SPX 0dte + all, SPY all, 0DTE imbalance."""
    with _state_lock:
        state["poll_count"] += 1
    errors = []
    for key_name, fn in (
        ("spx_0dte", lambda: fetch_levels("SPX", "0dte")),
        ("spx_all", lambda: fetch_levels("SPX", "all")),
        ("spy_all", lambda: fetch_levels("SPY", "all")),
        ("imbalance", lambda: fetch_imbalance("SPX")),
    ):
        try:
            value = fn()
            with _state_lock:
                state[key_name] = value
            log(f"poll ok {key_name}: spot={value.get('spot')} as_of={value.get('as_of') or value.get('timestamp')}")
        except Exception as exc:
            errors.append(f"{key_name}: {exc}")
            log(f"poll failed {key_name}: {exc}")
    now = datetime.now(timezone.utc).isoformat()
    with _state_lock:
        state["last_fetch"] = now
        state["last_fetch_ok"] = not errors
        state["last_error"] = "; ".join(errors) if errors else None
    save_state()
    return not errors


def in_session(now_et=None):
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    mins = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def poller_loop():
    log("poller started")
    while True:
        try:
            if not zerogex_key():
                log("poller waiting: zerogex not configured (set ZEROGEX_API_KEY in Coolify)")
                time.sleep(300)
                continue
            poll_once()
        except Exception as exc:
            log(f"poller cycle error: {exc}")
        time.sleep(IN_SESSION_POLL if in_session() else OUT_SESSION_POLL)


# ---------------------------------------------------------------- scheduler
# User-chosen schedule: morning 08:30 + close 14:30 ET, weekdays only.
# No noon, no hourly, no overnight. Each slot fires once per day, inside
# a 5-minute window (so a slow fetch or a restart at 08:30 does not lose
# the alert). A failed send is NOT marked sent, so it retries each minute
# inside the window, then gives up until the next day.
SCHEDULE = (("morning", 8, 30), ("close", 14, 30))
SCHEDULE_WINDOW_MINUTES = 5


def _send_scheduled(slot, levels):
    """Build + send one scheduled alert. Returns True only if Discord accepted it."""
    if slot == "morning":
        msg = build_morning_message(levels)
    else:
        msg = build_close_message(levels)
    result = post_discord(msg)
    return bool(result.get("sent"))


def maybe_send_scheduled(now_et=None):
    """Check the schedule once. Returns the slot sent, or None.

    Testable on its own: pass a datetime in America/New_York.
    """
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return None
    for slot, hour, minute in SCHEDULE:
        slot_start = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_min = (now_et - slot_start).total_seconds() / 60.0
        if not (0 <= delta_min < SCHEDULE_WINDOW_MINUTES):
            continue
        key = f"{now_et.date().isoformat()} {slot}"
        with _state_lock:
            if state.get("sent_slots", {}).get(key):
                return None
            levels = state.get("spx_0dte")
        if not levels and zerogex_key():
            log(f"scheduler {slot}: no cached levels, fetching now")
            try:
                poll_once()
            except Exception as exc:
                log(f"scheduler {slot} fetch failed: {exc}")
            with _state_lock:
                levels = state.get("spx_0dte")
        if not levels:
            log(f"scheduler {slot}: no levels available, will retry inside the window")
            return None
        if _send_scheduled(slot, levels):
            with _state_lock:
                state.setdefault("sent_slots", {})[key] = True
                # keep only the last 14 days of slot marks
                state["sent_slots"] = dict(sorted(state["sent_slots"].items())[-28:])
            save_state()
            log(f"scheduler: sent {slot} alert for {now_et.date().isoformat()}")
            return slot
        log(f"scheduler {slot}: Discord send failed, will retry inside the window")
        return None
    return None


def scheduler_loop():
    log("scheduler started: morning 08:30 ET, close 14:30 ET, weekdays only")
    while True:
        try:
            maybe_send_scheduled()
        except Exception as exc:
            log(f"scheduler error: {exc}")
        time.sleep(30)


# ---------------------------------------------------------------- Discord
def post_discord(content):
    """POST plain text to the Discord webhook. Returns dict, never raises."""
    url = discord_webhook()
    if not url:
        log("discord not configured (DISCORD_WEBHOOK_URL missing) - message not sent")
        return {"configured": False, "sent": False, "reason": "discord not configured"}
    payload = {"content": content}
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(payload, fh)
            tmp = fh.name
        proc = subprocess.run(
            ["curl", "-s", "-m", "15", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-H", "User-Agent: curl/8.5.0",
             "-d", f"@{tmp}", "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=30)
        os.unlink(tmp)
        out = proc.stdout
        lines = out.rsplit("\n", 1)
        status = int(lines[1]) if len(lines) == 2 and lines[1].isdigit() else 0
        if 200 <= status < 300:
            log("discord message sent")
            return {"configured": True, "sent": True}
        log(f"discord post failed HTTP {status}: {out[:300]}")
        return {"configured": True, "sent": False, "reason": f"discord HTTP {status}"}
    except Exception as exc:
        log(f"discord post error: {exc}")
        return {"configured": True, "sent": False, "reason": str(exc)}


def fmt_money(value):
    if value is None:
        return "not clear today"
    try:
        return f"${float(value):,.0f}"
    except Exception:
        return str(value)


def _net_gex_number(levels):
    """Best available net-GEX figure for the 0DTE book (may be None)."""
    if not levels:
        return None
    v = levels.get("net_gex_at_spot")
    if v is not None:
        return float(v)
    v = levels.get("net_gex_profile_sum")
    return float(v) if v is not None else None


def _age_text(levels):
    as_of = (levels or {}).get("as_of")
    if not as_of:
        return "time not shown"
    try:
        dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        age = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
        return f"about {age} minutes old" if age >= 1 else "just updated"
    except Exception:
        return f"as of {as_of}"


def build_recommendation(levels):
    """One plain-English recommendation: try buys, try sells, or wait.

    User requirement 2026-11-24: add a recommendation like calls / puts /
    choppy / don't try / wait to the alert. Rules, in priority order:
      - near ceiling: don't chase buys up here, wait for pullback or break-and-hold
      - near floor: don't chase sells down here, wait for bounce or break-and-hold
      - mood line not clear + choppy: don't force a trade, wait for a clear move
      - above mood line: buys look better than sells, look for buys on a pullback
      - below mood line: sells look better than buys, look for sells on a bounce
      - near drift-toward price: may stay quiet, wait for a clear move away
      - otherwise: no clear lean, wait for a move toward ceiling or floor
    No jargon, no scores, no percentages. One or two short sentences.
    """
    if not levels:
        return "Wait - levels are not clear yet today, don't force a trade."
    spot = levels.get("spot")
    call_wall = levels.get("call_wall")
    put_wall = levels.get("put_wall")
    max_pain = levels.get("max_pain")
    flip = levels.get("gamma_flip")
    net = _net_gex_number(levels)
    try:
        spot_f = float(spot) if spot is not None else None
        call_f = float(call_wall) if call_wall is not None else None
        floor_f = float(put_wall) if put_wall is not None else None
        pain_f = float(max_pain) if max_pain is not None else None
        flip_f = float(flip) if flip is not None else None
    except Exception:
        return "Wait - levels are not clear yet today, don't force a trade."
    if spot_f is not None and call_f is not None and abs(spot_f - call_f) <= 50:
        return "Don't chase buys up here. Wait for price to pull back, or wait for it to break above the ceiling and stay there."
    if spot_f is not None and floor_f is not None and abs(spot_f - floor_f) <= 50:
        return "Don't chase sells down here. Wait for price to bounce up, or wait for it to break below the floor and stay there."
    if flip_f is None and net is not None and net < 0:
        return "Choppy today, don't force a trade. Wait for a clear move up or down."
    if flip_f is not None and spot_f is not None:
        if spot_f > flip_f:
            return "Buys look better than sells today. Look for buys on a pullback, don't chase sells."
        return "Sells look better than buys today. Look for sells on a bounce, don't chase buys."
    if spot_f is not None and pain_f is not None and abs(spot_f - pain_f) <= 25:
        return "Price is near where it tends to drift toward, so it may stay quiet. Wait for a clear move away."
    return "No clear lean today. Wait for price to move toward the ceiling or floor before picking a side."


def build_morning_message(levels, imbalance=None):
    """Plain-English morning alert, under ~12 lines. No jargon."""
    today = datetime.now(ET).strftime("%a %b %d, %Y")
    spot = levels.get("spot") if levels else None
    call_wall = levels.get("call_wall") if levels else None
    put_wall = levels.get("put_wall") if levels else None
    max_pain = levels.get("max_pain") if levels else None
    flip = levels.get("gamma_flip") if levels else None
    net = _net_gex_number(levels)

    flip_line = fmt_money(flip) if flip is not None else "not clear today"

    # What this means: 1-2 plain sentences, based on position vs ceiling/floor.
    meaning_parts = []
    if spot is not None and call_wall is not None and put_wall is not None:
        near_ceiling = abs(float(spot) - float(call_wall)) <= 25
        near_floor = abs(float(spot) - float(put_wall)) <= 25
        if near_ceiling:
            meaning_parts.append("Price is near the ceiling, so be careful buying up here.")
        elif near_floor:
            meaning_parts.append("Price is near the floor, so be careful selling down here.")
        else:
            meaning_parts.append("Price is in the middle, no strong push either way.")
    if flip is None:
        meaning_parts.append("The mood line is not clear today, so take the ceiling and floor as a rough range only.")
    elif spot is not None:
        if float(spot) > float(flip):
            meaning_parts.append("Price is above the mood line, so buyers have the upper hand for now.")
        else:
            meaning_parts.append("Price is below the mood line, so sellers have the upper hand for now.")
    meaning = " ".join(meaning_parts) or "Levels are not clear yet today."

    # Watch out for: one plain warning if relevant.
    warning = None
    if net is not None and net < 0:
        warning = "Moves can get bigger and choppier today."
    elif net is not None and net > 0:
        warning = "Moves tend to stay calmer today."

    lines = [
        f"SPX today - {today}",
        f"Price now: {fmt_money(spot)}",
        f"Ceiling to watch: {fmt_money(call_wall)}",
        f"Floor to watch: {fmt_money(put_wall)}",
        f"Price tends to drift toward: {fmt_money(max_pain)}",
        f"Mood line: {flip_line}",
        f"What this means: {meaning}",
        f"Recommendation: {build_recommendation(levels)}",
    ]
    if warning:
        lines.append(f"Watch out for: {warning}")
    lines.append(f"Levels from {_age_text(levels)}.")
    return "\n".join(lines)


def build_close_message(levels):
    """Plain-English 14:30 close alert, under ~12 lines. No jargon.

    Where price is vs the price it tends to drift toward, and what to
    watch into the close. No scores, no percentages, no pillar names.
    """
    today = datetime.now(ET).strftime("%a %b %d, %Y")
    spot = levels.get("spot") if levels else None
    call_wall = levels.get("call_wall") if levels else None
    put_wall = levels.get("put_wall") if levels else None
    max_pain = levels.get("max_pain") if levels else None
    flip = levels.get("gamma_flip") if levels else None
    flip_line = fmt_money(flip) if flip is not None else "not clear today"

    if spot is not None and max_pain is not None:
        diff = float(spot) - float(max_pain)
        if abs(diff) <= 25:
            drift_line = "Price is close to where it tends to drift toward."
            watch = "It may stay quiet into the close."
        elif diff < 0:
            drift_line = f"Price is ${abs(diff):,.0f} below where it tends to drift toward."
            watch = "Watch for a slow move up into the close."
        else:
            drift_line = f"Price is ${diff:,.0f} above where it tends to drift toward."
            watch = "Watch for a slow move down into the close."
    else:
        drift_line = "Where price tends to drift toward is not clear today."
        watch = "Trade the ceiling and floor as a rough range into the close."

    if spot is not None and call_wall is not None and abs(float(spot) - float(call_wall)) <= 25:
        watch += " Price is near the ceiling, so be careful buying up here."
    elif spot is not None and put_wall is not None and abs(float(spot) - float(put_wall)) <= 25:
        watch += " Price is near the floor, so be careful selling down here."

    lines = [
        f"SPX into the close - {today}",
        f"Price now: {fmt_money(spot)}",
        f"Price tends to drift toward: {fmt_money(max_pain)}",
        drift_line,
        f"Ceiling to watch: {fmt_money(call_wall)}",
        f"Floor to watch: {fmt_money(put_wall)}",
        f"Mood line: {flip_line}",
        f"What to watch: {watch}",
        f"Recommendation: {build_recommendation(levels)}",
        f"Levels from {_age_text(levels)}.",
    ]
    return "\n".join(lines)


def build_gate_message(direction, price, decision, reason, levels, flip_used, flip_source):
    """Plain-English PASSED / BLOCKED alert, under ~12 lines. No jargon."""
    call_wall = levels.get("call_wall") if levels else None
    put_wall = levels.get("put_wall") if levels else None
    flip_line = fmt_money(flip_used) if flip_used is not None else "not clear today"
    if decision == "PASSED":
        if direction == "CALL":
            rec = "Try the buy, 1 contract only. If it turns back below the mood line, get out, don't hold and hope."
        else:
            rec = "Try the sell, 1 contract only. If it turns back above the mood line, get out, don't hold and hope."
    else:
        rec = "Don't try this one, wait for the next clear setup."
    lines = [
        f"{decision} - {direction} at {fmt_money(price)}",
        f"Why: {reason}",
        f"Recommendation: {rec}",
        f"Ceiling to watch: {fmt_money(call_wall)}",
        f"Floor to watch: {fmt_money(put_wall)}",
        f"Mood line: {flip_line}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- gate logic
def _fresh_levels():
    """Return (levels, error). Levels must have been fetched <10 min ago."""
    with _state_lock:
        levels = state.get("spx_0dte")
    if not levels:
        return None, "no levels yet - poller has not fetched"
    fetched = levels.get("fetched_at")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched.replace("Z", "+00:00"))).total_seconds()
    except Exception:
        return None, "levels have no usable fetch time"
    if age > FRESH_SECONDS:
        return None, f"stale - levels are {int(age // 60)} minutes old"
    return levels, None


def gate_alert(payload):
    """Apply the gate. Returns (http_status, response_dict)."""
    direction = str(payload.get("direction") or "").strip().upper()
    price = payload.get("price")
    symbol = str(payload.get("symbol") or "SPX").strip().upper()
    if direction not in ("CALL", "PUT"):
        return 400, {"error": "direction must be CALL or PUT"}
    try:
        price = float(price)
    except Exception:
        return 400, {"error": "price must be a number"}
    if symbol != "SPX":
        return 400, {"error": "only SPX is supported by this gate"}

    levels, err = _fresh_levels()
    if levels is None:
        log(f"gate: no gate - {err} (direction={direction} price={price})")
        return 503, {"decision": "NO_GATE", "error": f"stale - no gate: {err}",
                     "note": "Not forwarded as passed. Fix the poller / API key and retry."}

    # Mood line: SPX 0DTE flip first; SPY flip as fallback; else skip the check.
    # SPY is priced ~SPX/10, so a SPY flip must be scaled to SPX terms
    # (731.01 in SPY dollars is ~7310 in SPX dollars). Using it raw would
    # make every CALL pass and every PUT block, so scale it, never use raw.
    flip = levels.get("gamma_flip")
    flip_source = "SPX today"
    if flip is None:
        with _state_lock:
            spy = state.get("spy_all")
        spy_flip = (spy or {}).get("gamma_flip")
        if spy_flip is not None:
            spy_spot = (spy or {}).get("spot")
            spx_spot = levels.get("spot")
            scale = (float(spx_spot) / float(spy_spot)) if spx_spot and spy_spot else 10.0
            flip = float(spy_flip) * scale
            flip_source = f"SPY fallback (x{scale:.2f})"
        else:
            flip_source = "not clear today"
    if flip is not None:
        flip = float(flip)

    call_wall = levels.get("call_wall")
    put_wall = levels.get("put_wall")
    call_wall = float(call_wall) if call_wall is not None else None
    put_wall = float(put_wall) if put_wall is not None else None

    # Wall check first: a price sitting on the ceiling/floor gets that
    # (more specific) reason, rather than the mood-line reason.
    decision, reason = "PASSED", None
    if direction == "CALL":
        if call_wall is not None and abs(price - call_wall) <= WALL_BUFFER:
            decision = "BLOCKED"
            reason = "Price is already at the ceiling, risky to buy here."
        elif flip is not None and price <= flip:
            decision = "BLOCKED"
            reason = "Price is below the mood line, so buying here goes against the current mood."
        else:
            reason = "Price is above the mood line and still below the ceiling, so this fits."
    else:  # PUT
        if put_wall is not None and abs(price - put_wall) <= WALL_BUFFER:
            decision = "BLOCKED"
            reason = "Price is already at the floor, risky to sell here."
        elif flip is not None and price >= flip:
            decision = "BLOCKED"
            reason = "Price is above the mood line, so selling here goes against the current mood."
        else:
            reason = "Price is below the mood line and still above the floor, so this fits."
    if flip is None and decision == "PASSED":
        reason += " The mood line is not clear today, so this is only about the ceiling and floor."

    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol, "direction": direction, "price": price,
        "decision": decision, "reason": reason,
        "flip": flip, "flip_source": flip_source,
        "call_wall": call_wall, "put_wall": put_wall,
        "as_of": levels.get("as_of"),
    }
    with _state_lock:
        state["decisions"].insert(0, record)
        state["decisions"] = state["decisions"][:20]
    save_state()
    log(f"gate {decision}: {direction} at {price} - {reason} (flip={flip} via {flip_source}, ceiling={call_wall}, floor={put_wall})")

    msg = build_gate_message(direction, price, decision, reason, levels, flip, flip_source)
    discord = post_discord(msg)
    return 200, {**record, "discord": discord, "message": msg}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep stdout clean; we log ourselves
        pass

    def _send_json(self, status, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode())
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path == "/health":
            with _state_lock:
                snap = {k: state[k] for k in ("last_fetch", "last_fetch_ok", "last_error", "poll_count")}
            self._send_json(200, {
                "status": "ok", "symbol": "SPX", **snap,
                "zerogex_configured": bool(zerogex_key()),
                "discord_configured": bool(discord_webhook()),
                "in_session": in_session(),
                "schedule": "morning 08:30 ET + close 14:30 ET weekdays, live gate alerts only; no noon/hourly/overnight",
            })
        elif path == "/":
            body = ("zerogex-alerts - SPX levels poller + ChartGT alert gate\n"
                    "GET /health\nGET /levels?symbol=SPX&expiration=0dte|all\n"
                    "POST /webhook/chartgt  {\"symbol\":\"SPX\",\"direction\":\"CALL|PUT\",\"price\":7237}\n"
                    "POST /alert/morning\nPOST /alert/close\nPOST /alert/test\n"
                    "Schedule: morning 08:30 ET + close 14:30 ET weekdays, live gate alerts only.\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/levels":
            symbol = (qs.get("symbol") or ["SPX"])[0].upper()
            expiration = (qs.get("expiration") or ["0dte"])[0].lower()
            with _state_lock:
                if symbol == "SPY":
                    levels = state.get("spy_all")
                elif expiration == "all":
                    levels = state.get("spx_all")
                else:
                    levels = state.get("spx_0dte")
                last_fetch = state.get("last_fetch")
            if not levels:
                self._send_json(503, {"error": "no data yet",
                                      "reason": "poller has not fetched levels yet - check ZEROGEX_API_KEY",
                                      "last_fetch": last_fetch})
                return
            self._send_json(200, levels)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._read_json()
        if path == "/webhook/chartgt":
            secret = webhook_secret()
            if secret:
                got = (self.headers.get("X-Webhook-Secret") or "") or (payload.get("secret") or "")
                if got != secret:
                    self._send_json(401, {"error": "bad webhook secret"})
                    return
            status, obj = gate_alert(payload)
            self._send_json(status, obj)
        elif path == "/alert/morning":
            with _state_lock:
                levels = state.get("spx_0dte")
            if not levels and zerogex_key():
                log("morning alert: no cached levels, fetching now")
                try:
                    poll_once()
                except Exception as exc:
                    log(f"morning alert fetch failed: {exc}")
                with _state_lock:
                    levels = state.get("spx_0dte")
            if not levels:
                self._send_json(503, {"error": "no levels available",
                                      "reason": "poller has not fetched levels yet - check ZEROGEX_API_KEY"})
                return
            with _state_lock:
                imbalance = state.get("imbalance")
            msg = build_morning_message(levels, imbalance)
            discord = post_discord(msg)
            self._send_json(200, {"sent": discord.get("sent"), "discord": discord, "message": msg})
        elif path == "/alert/close":
            with _state_lock:
                levels = state.get("spx_0dte")
            if not levels and zerogex_key():
                log("close alert: no cached levels, fetching now")
                try:
                    poll_once()
                except Exception as exc:
                    log(f"close alert fetch failed: {exc}")
                with _state_lock:
                    levels = state.get("spx_0dte")
            if not levels:
                self._send_json(503, {"error": "no levels available",
                                      "reason": "poller has not fetched levels yet - check ZEROGEX_API_KEY"})
                return
            msg = build_close_message(levels)
            discord = post_discord(msg)
            self._send_json(200, {"sent": discord.get("sent"), "discord": discord, "message": msg})
        elif path == "/alert/test":
            msg = "Test OK - zerogex-alerts is working. You will get morning and trade alerts here."
            discord = post_discord(msg)
            self._send_json(200, {"sent": discord.get("sent"), "discord": discord, "message": msg})
        else:
            self._send_json(404, {"error": "not found"})


def main():
    load_state()
    log(f"zerogex-alerts starting on port {PORT} "
        f"(zerogex_configured={bool(zerogex_key())}, discord_configured={bool(discord_webhook())})")
    if not zerogex_key():
        log("WARNING: ZEROGEX_API_KEY not set - poller will wait, /health still answers ok")
    if not discord_webhook():
        log("WARNING: DISCORD_WEBHOOK_URL not set - alerts will log 'discord not configured'")
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
