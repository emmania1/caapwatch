#!/usr/bin/env python3
"""
Panel 2 — GDP growth (+ Argentina inflation / FX).

Source: World Bank "World Development Indicators" JSON API. Free, no key, no
bot-challenge. We pull real GDP growth for CAAP's markets (Argentina, Brazil,
Italy, Armenia, Uruguay, Ecuador) plus two Argentina-specific macro series —
consumer-price inflation and the official ARS/USD exchange rate — because
Argentina is CAAP's largest market and its macro is the dominant swing factor.

The World Bank API returns `[meta, rows]`; rows are newest-year-first and recent
years are often `null` until reported, so we take the latest *non-null* value per
series and keep a short tail for the sparkline.

Resilience: builds the whole payload first and only writes if it validates, so a
bad/empty response leaves the last-known data in place (see common.write_json).
"""

import sys

from common import http_get_json, now_iso, write_json

GDP_INDICATOR = "NY.GDP.MKTP.KD.ZG"        # real GDP growth, annual %
CPI_INDICATOR = "FP.CPI.TOTL.ZG"           # inflation, consumer prices, annual %
FX_INDICATOR = "PA.NUS.FCRF"               # official exchange rate, LCU/US$, period avg

# Ordered by CAAP traffic weight (largest market first).
MARKETS = [
    ("ARG", "Argentina"), ("BRA", "Brazil"), ("ITA", "Italy"),
    ("ARM", "Armenia"), ("URY", "Uruguay"), ("ECU", "Ecuador"),
]

# How many trailing years of points to keep for each sparkline.
TAIL = 10
START_YEAR = 2010


def _api_url(codes, indicator):
    return (
        "https://api.worldbank.org/v2/country/" + codes
        + "/indicator/" + indicator
        + f"?format=json&per_page=600&date={START_YEAR}:2026"
    )


def fetch_series(codes, indicator):
    """Return {iso3: [{'year': '2019', 'value': 2.1}, ...]} sorted oldest→newest.

    Only non-null observations are kept (sparkline plots them evenly spaced).
    """
    payload = http_get_json(_api_url(codes, indicator))
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        raise ValueError("unexpected World Bank response for " + indicator)
    out = {}
    for row in payload[1]:
        iso3 = row.get("countryiso3code") or ""
        val = row.get("value")
        yr = row.get("date")
        if not iso3 or val is None or yr is None:
            continue
        out.setdefault(iso3, []).append({"year": yr, "value": round(float(val), 2)})
    for iso3 in out:
        out[iso3].sort(key=lambda p: p["year"])
        out[iso3] = out[iso3][-TAIL:]
    return out


def latest_point(series):
    return series[-1] if series else None


def build_payload():
    gdp = fetch_series(";".join(c for c, _ in MARKETS), GDP_INDICATOR)

    countries = []
    for iso3, name in MARKETS:
        s = gdp.get(iso3, [])
        last = latest_point(s)
        countries.append({
            "name": name,
            "iso3": iso3,
            "latest": last["value"] if last else None,
            "year": last["year"] if last else None,
            "series": s,
        })

    if sum(1 for c in countries if c["latest"] is not None) < 3:
        raise ValueError("too few GDP values parsed")

    # Argentina-only macro context.
    cpi = fetch_series("ARG", CPI_INDICATOR).get("ARG", [])
    fx = fetch_series("ARG", FX_INDICATOR).get("ARG", [])
    cpi_last, fx_last = latest_point(cpi), latest_point(fx)

    return {
        "updated_at": now_iso(),
        "status": "ok",
        "source": "World Bank — World Development Indicators",
        "source_url": "https://data.worldbank.org/indicator/" + GDP_INDICATOR,
        "indicator": "Real GDP growth (annual %)",
        "countries": countries,
        "argentina": {
            "inflation": {
                "label": "Inflation (CPI, annual %)",
                "latest": cpi_last["value"] if cpi_last else None,
                "year": cpi_last["year"] if cpi_last else None,
                "series": cpi,
            },
            "fx": {
                "label": "ARS / USD (official, annual avg)",
                "latest": fx_last["value"] if fx_last else None,
                "year": fx_last["year"] if fx_last else None,
                "series": fx,
            },
        },
    }


def main():
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001 - resilient: keep last-known on any failure
        print(f"[fetch_gdp] FAILED ({type(exc).__name__}: {exc}); keeping last-known data")
        return 0
    result = write_json("gdp.json", payload)
    if result is None:
        print("[fetch_gdp] no change — left existing gdp.json untouched")
    else:
        n = len(payload["countries"])
        print(f"[fetch_gdp] wrote gdp.json ({n} markets; latest yr "
              f"{payload['countries'][0]['year']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
