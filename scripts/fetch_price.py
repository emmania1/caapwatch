#!/usr/bin/env python3
"""
Header quote — CAAP share price (NYSE: CAAP).

Market context for the demand panels: where is the stock while the traffic /
concession story plays out. Free, no key — Stooq's live-quote CSV gives the
headline price (symbol caap.us), the same source/parser the fuel ribbon uses.

For the header sparkline we keep ~90 trading days of daily closes. Stooq's bulk
history download is now apikey-gated (captcha), so we BACKFILL the history from
Yahoo Finance's free v8 chart JSON (range=6mo, no key) and merge the live Stooq
quote in as the latest point — so the sparkline renders immediately and stays
rolling. If the backfill ever fails we fall back to the stored history plus
today's quote (self-building forward), so the panel still works.

Day-over-day change is computed against the previous *distinct-date* close in
that history; only on a true cold start (no history and no backfill) do we fall
back to the intraday open→close move so the chip is never blank. This is market
context only — NOT a recommendation; the dashboard is not investment advice.

Resilient: on any fetch/parse failure we keep the last-known price.json.
"""

import sys
from datetime import datetime, timezone

from common import (
    http_get_json,
    http_get_text,
    now_iso,
    read_existing,
    to_number,
    write_json,
)

STOOQ_QUOTE = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
SYMBOL = "caap.us"               # Stooq live-quote symbol
YH_SYMBOL = "CAAP"               # Yahoo Finance ticker (NYSE)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
HISTORY_CAP = 90                 # trading days kept for the sparkline


def fetch_quote(symbol):
    """Return {as_of, open, close} for a Stooq symbol, or None.

    CSV columns (f=sd2t2ohlcv): Symbol, Date, Time, Open, High, Low, Close, Volume.
    """
    csv_text = http_get_text(STOOQ_QUOTE.format(sym=symbol))
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    cells = lines[1].split(",")
    if len(cells) < 7:
        return None
    date = cells[1].strip()
    op, close = to_number(cells[3]), to_number(cells[6])
    if close is None or close <= 0:
        return None
    return {"as_of": date, "open": op, "close": close}


def fetch_history(symbol, days=HISTORY_CAP):
    """Return [{date, close}, ...] oldest→newest from Yahoo's v8 chart JSON, or None.

    Free, no key. We use the raw `close` series (not adjclose) so the points line
    up with Stooq's quote close. Null closes (holidays/halts) are skipped.
    """
    data = http_get_json(YAHOO_CHART.format(sym=symbol))
    result = (((data or {}).get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    by_date = {}
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        by_date[day] = round(float(close), 2)      # last wins if a date repeats
    points = [{"date": d, "close": by_date[d]} for d in sorted(by_date)]
    return points[-days:] or None


def merge_history(prev, yhist, q):
    """Combine stored history + Yahoo backfill + the live Stooq quote into one
    deduped, date-sorted, capped series.

    Later sources override earlier ones for a shared date: Yahoo (authoritative
    EOD) over stored, the live quote over Yahoo for its own session.
    """
    merged = {}
    for p in (prev or {}).get("history") or []:
        if p.get("date") and p.get("close") is not None:
            merged[p["date"]] = round(float(p["close"]), 2)
    for p in yhist or []:
        merged[p["date"]] = p["close"]
    if q and q.get("as_of") and q.get("close") is not None:
        merged[q["as_of"]] = round(float(q["close"]), 2)
    points = [{"date": d, "close": merged[d]} for d in sorted(merged) if d]
    return points[-HISTORY_CAP:]


def prior_close(history, as_of):
    """Most recent close from a DIFFERENT date than as_of (day-over-day base)."""
    for p in reversed(history or []):
        if p.get("date") != as_of and p.get("close"):
            return p["close"]
    return None


def build_payload():
    prev = read_existing("price.json") or {}

    # Live quote (Stooq) — intraday-ish latest; also our cold-start fallback.
    try:
        q = fetch_quote(SYMBOL)
    except Exception as exc:  # noqa: BLE001 - Yahoo history may still carry the panel
        print(f"[fetch_price] live quote failed ({type(exc).__name__}); relying on history")
        q = None

    # Daily history backfill (Yahoo v8, no key) — seeds & refreshes the sparkline.
    try:
        yhist = fetch_history(YH_SYMBOL, days=HISTORY_CAP)
    except Exception as exc:  # noqa: BLE001 - fall back to stored + live quote
        print(f"[fetch_price] history backfill failed ({type(exc).__name__}); using stored history")
        yhist = None

    history = merge_history(prev, yhist, q)
    if not history:
        raise ValueError("no CAAP price from Stooq, Yahoo, or stored history")

    latest = history[-1]
    as_of, price = latest["date"], latest["close"]

    base = prior_close(history, as_of)
    basis = "prior close" if base else "intraday"
    if not base and q:                      # true cold start: one session only
        base = q.get("open")

    change = change_pct = None
    if base:
        change = round(price - base, 2)
        change_pct = round((price - base) / base * 100, 2)

    return {
        "updated_at": now_iso(),
        "status": "ok",
        "source": "Stooq + Yahoo Finance" if yhist else "Stooq",
        "source_url": "https://stooq.com/q/?s=caap.us",
        "symbol": "CAAP",
        "exchange": "NYSE",
        "currency": "USD",
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "change_basis": basis,
        "as_of": as_of,
        "history": history,
    }


def main():
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001 - resilient: keep last-known on any failure
        print(f"[fetch_price] FAILED ({type(exc).__name__}: {exc}); keeping last-known data")
        return 0
    result = write_json("price.json", payload)
    if result is None:
        print("[fetch_price] no change — left existing price.json untouched")
    else:
        print(f"[fetch_price] wrote price.json (CAAP ${payload['price']} "
              f"{payload['change_pct']}% vs {payload['change_basis']}; "
              f"{len(payload['history'])} history pts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
