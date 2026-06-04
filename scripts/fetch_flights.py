"""
CAAPWATCH flight engine — nowcasts CAAP traffic from ADS-B movement data
(OpenSky Network) at the key concession airports, ahead of the company's own
monthly release.

This is a DEVELOPING signal, built forward over time — not a point-in-time
reading. Each daily run measures a short, recent window OpenSky's free tier
reliably serves (last ~24h) and APPENDS that count to a per-airport rolling
history in data/flights.json, exactly the way fetch_price.py grows its sparkline.
A trend therefore accumulates run-over-run; meaningful readings emerge over weeks.

We deliberately do NOT pull year-ago history for a YoY number: OpenSky's free
tier doesn't serve it (every such pull came back empty), so the honest signal is
the forward-built trend, not a vs-last-year delta.

What it produces (data/flights.json), per airport:
  - history[]: dated movement counts (arrivals+departures), oldest→newest
  - latest: the most recent dated snapshot, plus —
  - Brazil (Brasília): that snapshot's movements split by carrier (GOL/LATAM/Azul)
    — the volume-risk signal for ask #4
  - Yerevan: that snapshot's arrivals originating in Russia — the Armenia
    rerouting tailwind for ask #5

Movements are real, measured counts. We deliberately do NOT convert them to a
passenger number, because seats-per-movement is an assumption, not data — the
movement trend itself is the signal. (A seat estimate can be layered on later
as an explicitly-flagged assumption.)

Auth: OpenSky now uses OAuth2 client credentials. Set OPENSKY_CLIENT_ID and
OPENSKY_CLIENT_SECRET as repo secrets (free account). Without them the script
falls back to anonymous access, which is heavily rate-limited and may only
cover a subset of airports — fine for a first smoke test, not for production.
"""
import os
import sys
import json
import time
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

from caap_config import (
    AIRPORTS, CARRIER_PREFIXES, RUSSIA_AIRPORTS,
)

OPENSKY_BASE = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network/"
             "protocol/openid-connect/token")

# We measure only a short recent window the free tier reliably serves, then build
# a trend by accumulating one dated point per daily run (see analyze). ~24h keeps
# each airport's pull tiny (a 24h span is at most two midnight-aligned day-chunks),
# which both respects OpenSky's 2-partition limit and sips the daily request
# budget so a full 9-airport run completes. Tunable via env without a code change.
WINDOW_HOURS = int(os.environ.get("OPENSKY_WINDOW_HOURS", "24"))

# OpenSky's free tier rate-limits bursts with HTTP 429 and has a daily request
# budget. We pace calls two ways: a short sleep before EVERY request, and a longer
# sleep BETWEEN airports, so the network as a whole completes a run instead of the
# first airport draining the burst allowance. Both tunable via env; the 429 handler
# in opensky_get backs off harder still if we trip the limit anyway.
REQUEST_SPACING_S = float(os.environ.get("OPENSKY_REQUEST_SPACING", "1.0"))
AIRPORT_SPACING_S = float(os.environ.get("OPENSKY_AIRPORT_SPACING", "3.0"))

# Rolling history kept per airport (one dated point per run ≈ one per day), so the
# sparkline shows roughly a quarter of trend — same idea as fetch_price's cap.
HISTORY_CAP = int(os.environ.get("OPENSKY_HISTORY_CAP", "120"))

# A pull can come back empty for two very different reasons: OpenSky genuinely saw
# no flights (a clean 404 — real data), or the call failed / was rate-limited /
# the airport is barely covered (no answer). The second must NOT be recorded as a
# count. opensky_get bumps this counter on every HARD failure (never on a clean
# 404) so analyze() records a dated point ONLY for a pull that completed — never
# writing a fake zero from a failed run into the trend. Reset per run.
_HARD_FAILURES = 0

# Circuit breaker. OpenSky's free tier has a DAILY request budget; once it's spent
# every call 429s until the quota resets next day, and no amount of backoff helps.
# After a few hard 429-exhaustions in a run we trip this flag and stop making real
# calls — every remaining airport then instantly carries its prior history forward
# instead of grinding through minutes of dead backoff (a full run would otherwise
# hang ~90 min). When the budget is healthy this never trips (calls return 200, not
# 429). Both reset per run, in analyze().
_RL_EXHAUSTIONS = 0
_BUDGET_EXHAUSTED = False
RL_TRIP_AFTER = int(os.environ.get("OPENSKY_RL_TRIP_AFTER", "3"))


def get_token():
    """OAuth2 client-credentials token, or None for anonymous access."""
    cid = os.environ.get("OPENSKY_CLIENT_ID")
    secret = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not (cid and secret):
        return None
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode()
    try:
        req = urllib.request.Request(TOKEN_URL, data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("access_token")
    except Exception as e:  # noqa
        print(f"  token fetch failed ({e}); falling back to anonymous", file=sys.stderr)
        return None


def opensky_get(path, params, token):
    """GET a list of flight records from OpenSky.

    Returns [] both for a clean 404 ("no flights that day" — a real, empty result)
    and for a hard failure (non-404 HTTP, or retries exhausted on persistent 429s
    / network errors). The hard-failure paths bump _HARD_FAILURES so the caller can
    tell the two apart and refuse to trust an airport whose pull never completed.
    """
    global _HARD_FAILURES, _RL_EXHAUSTIONS, _BUDGET_EXHAUSTED
    # Budget already spent this run — skip the call instantly (still a hard failure,
    # so the airport stays "incomplete" and carries its prior history forward).
    if _BUDGET_EXHAUSTED:
        _HARD_FAILURES += 1
        return []
    url = f"{OPENSKY_BASE}{path}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    time.sleep(REQUEST_SPACING_S)  # pace calls to stay under OpenSky's burst rate-limit
    rate_limited = False
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r) or []
        except urllib.error.HTTPError as e:
            if e.code == 404:      # OpenSky returns 404 for "no flights" — a real empty
                return []
            if e.code == 429:      # rate limited — back off progressively and retry
                rate_limited = True
                time.sleep(10 * (attempt + 1))   # 10s, 20s, 30s
                continue
            print(f"  HTTP {e.code} for {path} {params.get('airport')}", file=sys.stderr)
            _HARD_FAILURES += 1
            return []
        except Exception as e:  # noqa
            time.sleep(5 * (attempt + 1))
    _HARD_FAILURES += 1   # retries exhausted (persistent 429 or network errors) — no data
    if rate_limited:
        _RL_EXHAUSTIONS += 1
        if _RL_EXHAUSTIONS >= RL_TRIP_AFTER and not _BUDGET_EXHAUSTED:
            _BUDGET_EXHAUSTED = True
            print("  OpenSky daily budget looks exhausted (repeated 429s) — skipping "
                  "remaining calls this run; histories carry forward.", file=sys.stderr)
    return []


def carrier_of(callsign):
    """Map an ADS-B callsign to a carrier group via its ICAO prefix."""
    if not callsign:
        return "Other"
    cs = callsign.strip().upper()
    for prefix, group in CARRIER_PREFIXES.items():
        if cs.startswith(prefix):
            return group
    return "Other"


def dedup(flights):
    """A flight can appear in both arrival and departure pulls; dedupe it."""
    seen, out = set(), []
    for f in flights:
        key = (f.get("icao24"), f.get("firstSeen"))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def carrier_split_of(flights):
    """Count this window's movements by carrier group (GOL / LATAM / Azul / …),
    largest first. No YoY — the trend lives in the per-day history, not a delta."""
    split = {}
    for f in flights:
        grp = carrier_of(f.get("callsign"))
        split[grp] = split.get(grp, 0) + 1
    return [{"carrier": g, "current": split[g]}
            for g in sorted(split, key=lambda g: -split[g])]


def append_point(history, point, cap=None):
    """Append a dated snapshot to a rolling history: dedup by date (a same-day
    re-run overwrites that day's point), keep oldest→newest, cap the length.

    This is the same merge fetch_price.py uses for its daily closes, so the panel
    can grow a movement trend run-over-run exactly like the price sparkline.
    """
    cap = HISTORY_CAP if cap is None else cap
    by_date = {p["date"]: p for p in history if p.get("date")}
    by_date[point["date"]] = point
    return [by_date[d] for d in sorted(by_date)][-cap:]


def day_chunks(begin, end):
    """Split [begin, end] into midnight-aligned UTC-day windows.

    OpenSky's history endpoint only lets a single query span ~2 day-partitions
    ("you can only query across 2 partitions ... your query will naturally spill
    into the 3rd day"), so a 7-day window can't be pulled in one request. Each
    chunk here lands inside a single UTC calendar day — so even counting the
    spill it touches at most 2 partitions — and the caller sums + dedups across
    chunks, reconstructing exactly what one wide query would have returned. The
    first and last chunks may be partial; interior chunks are full 24h days.
    """
    out = []
    t = int(begin)
    end = int(end)
    while t < end:
        next_midnight = (t // 86400) * 86400 + 86400
        stop = min(next_midnight, end)
        out.append((t, stop))
        t = stop
    return out


def collect_airport(icao, begin, end, token, fetch=opensky_get):
    """All arrivals+departures at an airport in [begin, end], deduped.

    Pulled one midnight-aligned UTC-day chunk at a time (see day_chunks) to stay
    inside OpenSky's 2-partition-per-query limit, then summed and deduped so the
    result matches a single wide query. Arrivals are returned separately (un-
    deduped, as before) for the Russia-inflow count; each arrival event falls in
    exactly one day-chunk, so chunking introduces no double-counting there.
    """
    movements, arrivals = [], []
    for cb, ce in day_chunks(begin, end):
        arr = fetch("/flights/arrival", {"airport": icao, "begin": cb, "end": ce}, token)
        dep = fetch("/flights/departure", {"airport": icao, "begin": cb, "end": ce}, token)
        arrivals += list(arr)
        movements += list(arr) + list(dep)
    return dedup(movements), arrivals


def analyze(fetch=opensky_get, now=None, prev=None):
    """Append this run's measured movement counts to each airport's rolling
    history and return the full flights.json payload.

    `fetch` is injectable for testing. `prev` is the previously-stored payload
    (read from data/flights.json) whose per-airport history we EXTEND; pass None
    on a cold start. We deliberately pull only ONE recent window — no year-ago
    comparison, which OpenSky's free tier doesn't serve — so the signal is the
    forward-built trend, not a YoY delta.
    """
    global _HARD_FAILURES, _RL_EXHAUSTIONS, _BUDGET_EXHAUSTED
    _HARD_FAILURES = 0
    _RL_EXHAUSTIONS = 0
    _BUDGET_EXHAUSTED = False
    token = get_token() if fetch is opensky_get else "TEST"
    now = now or dt.datetime.now(dt.timezone.utc)
    end = int(now.timestamp())
    begin = int((now - dt.timedelta(hours=WINDOW_HOURS)).timestamp())
    today = now.strftime("%Y-%m-%d")

    prev_by_icao = {a.get("icao"): a for a in (prev or {}).get("airports", [])}

    out_airports = []
    for ap in AIRPORTS:
        # Re-mint the OAuth token per airport so a long run never 401s partway
        # through on a token that expired mid-run. Gated to live runs, and skipped
        # once the budget is exhausted (no point minting tokens for calls we won't
        # make). The injected-fetch test path keeps its "TEST" sentinel untouched.
        if fetch is opensky_get and not _BUDGET_EXHAUSTED:
            token = get_token() or token
        icao = ap["icao"]
        fails_before = _HARD_FAILURES
        movements, arrivals = collect_airport(icao, begin, end, token, fetch)
        cur_n = len(movements)
        # A pull "completed" only if NO call for this airport hard-failed. We
        # record a dated point only for a completed pull — never writing a fake
        # zero from a rate-limited/failed run into the trend (which would invent
        # a dip). A failed airport simply carries its prior history forward.
        completed = (_HARD_FAILURES == fails_before)

        history = list((prev_by_icao.get(icao) or {}).get("history") or [])
        if completed:
            point = {"date": today, "movements": cur_n}
            # Brazil: split by carrier (ask #4 — GOL/LATAM/Azul volume risk)
            if ap.get("brazil_carrier_split"):
                point["carrier_split"] = carrier_split_of(movements)
            # Yerevan: arrivals originating in Russia (ask #5 — the tailwind)
            if ap.get("track_russia_inflow"):
                point["russia_inflow"] = {
                    "current": sum(1 for f in arrivals
                                   if f.get("estDepartureAirport") in RUSSIA_AIRPORTS),
                }
            history = append_point(history, point)

        out_airports.append({
            "icao": icao, "name": ap["name"], "country": ap["country"],
            "coverage": ap["coverage"],
            "latest": history[-1] if history else None,
            "history": history,
            "last_complete": completed,
        })

        # Space airports out so the whole network completes a run under OpenSky's
        # burst limit, rather than the first airport eating the allowance. Skipped
        # once the budget is exhausted — the remaining airports make no real calls.
        if fetch is opensky_get and not _BUDGET_EXHAUSTED and ap is not AIRPORTS[-1]:
            time.sleep(AIRPORT_SPACING_S)

    any_reading = any((a["latest"] or {}).get("movements", 0) > 0 for a in out_airports)
    return {
        "updated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "window_hours": WINDOW_HOURS,
        "history_days": HISTORY_CAP,
        "developing": True,
        "any_reading": any_reading,
        "source": ("OpenSky Network (ADS-B). Movements = arrivals + departures over "
                   "a recent window; a measured proxy for activity, not passengers. "
                   "Trend builds with each daily run."),
        "auth": "authenticated" if token and token != "TEST" else ("test" if token == "TEST" else "anonymous"),
        "airports": out_airports,
    }


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "..", "data", "flights.json")
    try:
        with open(out) as f:
            prev = json.load(f)
    except (FileNotFoundError, ValueError):
        prev = None   # cold start — history begins now

    payload = analyze(prev=prev)

    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    airports = payload["airports"]
    n_read = sum(1 for a in airports if (a.get("latest") or {}).get("movements", 0) > 0)
    print(f"wrote {out}: {len(airports)} airports, auth={payload['auth']}, "
          f"{n_read}/{len(airports)} returning a non-zero reading this run")
    for a in airports:
        mv = (a.get("latest") or {}).get("movements")
        flag = "" if a.get("last_complete") else "  (pull incomplete — carried prior history)"
        print(f"  {a['icao']:5} {a['name']:26} "
              f"movements={('-' if mv is None else mv):>5}  "
              f"history={len(a.get('history') or [])}pt{flag}")
