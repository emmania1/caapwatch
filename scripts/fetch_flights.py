"""
CAAPWATCH flight engine — nowcasts CAAP traffic from ADS-B movement data
(OpenSky Network) at the key concession airports, ahead of the company's own
monthly release.

What it produces (data/flights.json):
  - per airport: arrivals+departures this trailing week vs the same week a year
    ago, with YoY % (the leading read on the 6-8% the EBITDA bridge needs)
  - Brazil (Brasília): the same movements split by carrier (GOL / LATAM / Azul)
    — the volume-risk signal for ask #4
  - Yerevan: arrivals originating in Russia, vs a year ago — the Armenia
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
    AIRPORTS, CARRIER_PREFIXES, RUSSIA_AIRPORTS, WINDOW_DAYS,
)

OPENSKY_BASE = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network/"
             "protocol/openid-connect/token")

# OpenSky's free tier rate-limits bursts with HTTP 429. A full run fires ~260
# day-chunked calls, so we space them out to stay under the burst cap — a daily
# batch job has no reason to sprint, and a polite ~1 req/s keeps the account from
# rate-limiting *itself* (which empties every airport and forces the panel into
# its pending state). Tunable via env without a code change; the 429 handler in
# opensky_get still backs off harder if we trip the limit anyway.
REQUEST_SPACING_S = float(os.environ.get("OPENSKY_REQUEST_SPACING", "1.0"))


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
    """GET a list of flight records from OpenSky. Returns [] on any failure."""
    url = f"{OPENSKY_BASE}{path}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    time.sleep(REQUEST_SPACING_S)  # pace calls to stay under OpenSky's burst rate-limit
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r) or []
        except urllib.error.HTTPError as e:
            if e.code == 404:      # OpenSky returns 404 for "no flights"
                return []
            if e.code == 429:      # rate limited — back off
                time.sleep(8 * (attempt + 1))
                continue
            print(f"  HTTP {e.code} for {path} {params.get('airport')}", file=sys.stderr)
            return []
        except Exception as e:  # noqa
            time.sleep(3)
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


def analyze(fetch=opensky_get, now=None):
    """Build the flights.json payload. `fetch` is injectable for testing."""
    token = get_token() if fetch is opensky_get else "TEST"
    now = now or dt.datetime.now(dt.timezone.utc)
    end = int(now.timestamp())
    begin = int((now - dt.timedelta(days=WINDOW_DAYS)).timestamp())
    # same window one year earlier
    py_end = int((now - dt.timedelta(days=365)).timestamp())
    py_begin = int((now - dt.timedelta(days=365 + WINDOW_DAYS)).timestamp())

    out_airports = []
    for ap in AIRPORTS:
        # Re-mint the OAuth token at the top of each airport. The day-chunked
        # pulls make ~32 calls per airport and a full run is ~290 calls over many
        # minutes against OpenSky's (currently slow) backend — long enough that a
        # single token minted once up front expires partway through, 401-ing the
        # final airports. One fresh token per airport keeps every token young
        # (≈32 calls) without touching the request logic. Gated to live runs so
        # the injected-fetch test path keeps its "TEST" sentinel untouched.
        if fetch is opensky_get:
            token = get_token() or token
        icao = ap["icao"]
        cur, cur_arrivals = collect_airport(icao, begin, end, token, fetch)
        prior, prior_arrivals = collect_airport(icao, py_begin, py_end, token, fetch)
        cur_n, prior_n = len(cur), len(prior)
        yoy = round((cur_n - prior_n) / prior_n * 100, 1) if prior_n else None

        rec = {
            "icao": icao, "name": ap["name"], "country": ap["country"],
            "coverage": ap["coverage"],
            "movements_current": cur_n,
            "movements_prior_year": prior_n,
            "yoy_pct": yoy,
        }

        # Brazil: split by carrier (ask #4 — GOL/LATAM volume risk)
        if ap.get("brazil_carrier_split"):
            split_cur, split_py = {}, {}
            for f in cur:
                split_cur[carrier_of(f.get("callsign"))] = split_cur.get(carrier_of(f.get("callsign")), 0) + 1
            for f in prior:
                split_py[carrier_of(f.get("callsign"))] = split_py.get(carrier_of(f.get("callsign")), 0) + 1
            carriers = []
            for grp in sorted(set(split_cur) | set(split_py), key=lambda g: -split_cur.get(g, 0)):
                c, p = split_cur.get(grp, 0), split_py.get(grp, 0)
                carriers.append({
                    "carrier": grp, "current": c, "prior_year": p,
                    "yoy_pct": round((c - p) / p * 100, 1) if p else None,
                })
            rec["carrier_split"] = carriers

        # Yerevan: arrivals originating in Russia (ask #5 — the tailwind)
        if ap.get("track_russia_inflow"):
            ru_cur = sum(1 for f in cur_arrivals if f.get("estDepartureAirport") in RUSSIA_AIRPORTS)
            ru_py = sum(1 for f in prior_arrivals if f.get("estDepartureAirport") in RUSSIA_AIRPORTS)
            rec["russia_inflow"] = {
                "current": ru_cur, "prior_year": ru_py,
                "yoy_pct": round((ru_cur - ru_py) / ru_py * 100, 1) if ru_py else None,
            }
        out_airports.append(rec)

    return {
        "updated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "window_days": WINDOW_DAYS,
        "source": "OpenSky Network (ADS-B). Movements = arrivals + departures; a measured proxy for activity, not passengers.",
        "auth": "authenticated" if token and token != "TEST" else ("test" if token == "TEST" else "anonymous"),
        "airports": out_airports,
    }


if __name__ == "__main__":
    payload = analyze()
    out = os.path.join(os.path.dirname(__file__), "..", "data", "flights.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {out}: {len(payload['airports'])} airports, auth={payload['auth']}")
