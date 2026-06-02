#!/usr/bin/env python3
"""
Header quote — CAAP share price (NYSE: CAAP).

Market context for the demand panels: where is the stock while the traffic /
concession story plays out. Free, no key — Stooq's live-quote CSV, the same
source and parser the fuel ribbon already uses (symbol caap.us).

Like the fuel panel we keep our OWN short price history — one close per day,
deduped by date, capped — so the header sparkline grows a real trend straight
from the daily Action (Stooq's bulk history download is blocked to robots).

Day-over-day change is computed against the previous *distinct-date* close we
stored; on the very first run (no prior day yet) we fall back to the intraday
open→close move so the chip is never blank. This is market context only — NOT a
recommendation; the dashboard is not investment advice.

Resilient: on any fetch/parse failure we keep the last-known price.json.
"""

import sys

from common import http_get_text, now_iso, read_existing, to_number, write_json

STOOQ_QUOTE = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
SYMBOL = "caap.us"
HISTORY_CAP = 90


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


def update_history(prev, as_of, close):
    hist = list((prev or {}).get("history") or [])
    point = {"date": as_of, "close": close}
    if hist and hist[-1].get("date") == as_of:
        hist[-1] = point          # same session — refresh in place
    else:
        hist.append(point)
    return hist[-HISTORY_CAP:]


def prior_close(prev, as_of):
    """Most recent stored close from a DIFFERENT date (for day-over-day change)."""
    for p in reversed((prev or {}).get("history") or []):
        if p.get("date") != as_of and p.get("close"):
            return p["close"]
    return None


def build_payload():
    q = fetch_quote(SYMBOL)
    if not q:
        raise ValueError("no CAAP quote from Stooq")

    prev = read_existing("price.json") or {}
    base = prior_close(prev, q["as_of"])
    basis = "prior close" if base else "intraday"
    if not base:
        base = q["open"]

    change = change_pct = None
    if base:
        change = round(q["close"] - base, 2)
        change_pct = round((q["close"] - base) / base * 100, 2)

    history = update_history(prev, q["as_of"], q["close"])

    return {
        "updated_at": now_iso(),
        "status": "ok",
        "source": "Stooq",
        "source_url": "https://stooq.com/q/?s=caap.us",
        "symbol": "CAAP",
        "exchange": "NYSE",
        "currency": "USD",
        "price": q["close"],
        "change": change,
        "change_pct": change_pct,
        "change_basis": basis,
        "as_of": q["as_of"],
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
              f"{payload['change_pct']}% vs {payload['change_basis']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
