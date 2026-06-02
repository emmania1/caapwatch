#!/usr/bin/env python3
"""
Panel 4 — Fuel / capacity risk ribbon.

Two free, no-key inputs:
  * Oil prices from Stooq's live-quote CSV — Brent (CB.F), WTI (CL.F), and NY
    Harbor ULSD (HO.F) as the standard middle-distillate proxy for jet fuel
    (jet kerosene and ULSD track within pennies; a clean unattended jet-fuel
    spot feed is otherwise paywalled). Each quote carries the day's open/close,
    so we show an intraday direction without needing a history endpoint
    (Stooq's bulk history download is blocked to robots).
  * Capacity headlines from Google News RSS — airline capacity cuts / route
    suspensions across CAAP-relevant European and LatAm carriers.

We also keep our OWN short price history: each run appends today's Brent/WTI
close (deduped by date, capped), so the ribbon grows a real trend over time
straight from the daily Action — no third-party history needed.

The risk level is a transparent rule (stored in the JSON), not a black box:
elevated/high keys off the count of recent capacity headlines and the average
intraday oil move. This panel is a *signal* tied to Panel 1 (fuel up + capacity
cuts ⇒ traffic risk); per the brief it deliberately does NOT chase flight-level
real-time cancellation counts (paywalled, out of scope).
"""

import statistics
import sys

from common import http_get_text, now_iso, read_existing, to_number, fetch_news, write_json

STOOQ_QUOTE = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"

OIL = [
    {"name": "Brent", "symbol": "cb.f", "unit": "$/bbl"},
    {"name": "WTI", "symbol": "cl.f", "unit": "$/bbl"},
    {"name": "Jet fuel (NY ULSD proxy)", "symbol": "ho.f", "unit": "$/gal"},
]

NEWS_QUERY = (
    '(LATAM OR Gol OR Azul OR "Aerolineas Argentinas" OR Lufthansa OR KLM OR SAS OR Iberia) '
    '(capacity cut OR route suspension OR flight cancellations OR "scale back" OR grounded OR "outlook cut")'
)

# Light relevance gate: Google already targets the query; this drops strays.
KEEP_TERMS = [
    "airline", "airlines", "capacity", "route", "suspend", "suspension", "cancel",
    "grounded", "scale back", "cut", "reduce", "jet fuel", "fuel cost", "flights",
    "latam", "gol", "azul", "aerolineas", "aerolíneas", "lufthansa", "klm", "sas", "iberia",
]

HISTORY_CAP = 60
MAX_HEADLINES = 6


def fetch_quote(symbol):
    """Return {price, open, change_pct, as_of} for a Stooq symbol, or None."""
    csv_text = http_get_text(STOOQ_QUOTE.format(sym=symbol))
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    cells = lines[1].split(",")
    if len(cells) < 7:
        return None
    date = cells[1].strip()
    op, close = to_number(cells[3]), to_number(cells[6])
    if close is None:
        return None
    change_pct = None
    if op not in (None, 0):
        change_pct = round((close - op) / op * 100, 2)
    return {"price": close, "open": op, "change_pct": change_pct, "as_of": date}


def build_oil():
    out = []
    for spec in OIL:
        try:
            q = fetch_quote(spec["symbol"])
        except Exception as exc:  # noqa: BLE001 - one symbol failing must not sink the panel
            print(f"[fetch_fuel] quote {spec['symbol']} failed: {type(exc).__name__}")
            q = None
        if q:
            out.append({**spec, **q})
    # Require at least the two crude benchmarks to consider the fetch good.
    have = {o["name"] for o in out}
    if not ({"Brent", "WTI"} <= have):
        raise ValueError("missing Brent/WTI crude quotes")
    return out


def update_history(oil):
    prev = read_existing("fuel.json") or {}
    hist = list(prev.get("history") or [])
    by = {o["name"]: o for o in oil}
    brent = by.get("Brent", {}).get("price")
    wti = by.get("WTI", {}).get("price")
    today = by.get("Brent", {}).get("as_of") or now_iso()[:10]
    point = {"date": today, "brent": brent, "wti": wti}
    if hist and hist[-1].get("date") == today:
        hist[-1] = point
    else:
        hist.append(point)
    return hist[-HISTORY_CAP:]


def relevant(title):
    t = title.lower()
    return any(term in t for term in KEEP_TERMS)


def fetch_headlines():
    try:
        items = fetch_news(NEWS_QUERY, max_items=14)
    except Exception as exc:  # noqa: BLE001 - news is best-effort; fall back to last-known
        print(f"[fetch_fuel] news fetch failed: {type(exc).__name__}; reusing last-known headlines")
        prev = read_existing("fuel.json") or {}
        return list(prev.get("headlines") or []), int(prev.get("risk", {}).get("headline_count", 0) or 0)
    seen, kept = set(), []
    for it in items:
        if not relevant(it["title"]):
            continue
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(it)
    kept.sort(key=lambda x: x.get("published_iso") or "", reverse=True)
    return kept[:MAX_HEADLINES], len(kept)


def assess_risk(oil, headline_count):
    changes = [o["change_pct"] for o in oil if o.get("change_pct") is not None]
    avg = round(statistics.fmean(changes), 2) if changes else 0.0
    trend = "up" if avg >= 1.0 else "down" if avg <= -1.0 else "flat"
    level = "watch"
    if headline_count >= 3 or avg >= 2.0:
        level = "elevated"
    if headline_count >= 6 or avg >= 4.0:
        level = "high"
    return {
        "level": level,
        "oil_trend": trend,
        "avg_change_pct": avg,
        "headline_count": headline_count,
        "rule": "elevated if ≥3 recent capacity headlines or avg oil +2%; high if ≥6 or +4%.",
    }


def build_payload():
    oil = build_oil()
    history = update_history(oil)
    headlines, hcount = fetch_headlines()
    risk = assess_risk(oil, hcount)
    return {
        "updated_at": now_iso(),
        "status": "ok",
        "source": "Stooq (oil) · Google News (capacity)",
        "oil_source_url": "https://stooq.com/q/?s=cb.f",
        "news_source_url": "https://news.google.com/search?q=airline%20capacity%20cut",
        "oil": oil,
        "history": history,
        "risk": risk,
        "headlines": headlines,
    }


def main():
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001 - resilient: keep last-known on any failure
        print(f"[fetch_fuel] FAILED ({type(exc).__name__}: {exc}); keeping last-known data")
        return 0
    result = write_json("fuel.json", payload)
    if result is None:
        print("[fetch_fuel] no change — left existing fuel.json untouched")
    else:
        print(f"[fetch_fuel] wrote fuel.json (risk={payload['risk']['level']}, "
              f"{len(payload['oil'])} prices, {len(payload['headlines'])} headlines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
