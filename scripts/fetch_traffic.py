#!/usr/bin/env python3
"""
Panel 1 — Traffic by Market.

Source: SEC EDGAR. Corporación América Airports (CIK 1717393) furnishes each
monthly passenger-traffic press release as a 6-K with the release text in its
EX-99.1 exhibit (identical numbers to the PDF on the IR site, but served as
clean HTML over a free JSON API with no key and no WAF/bot challenge).

Flow:
  1. data.sec.gov submissions JSON  -> list recent 6-K filings (newest first)
  2. each filing's index.json       -> find the EX-99.1 exhibit
  3. first exhibit that is a traffic release -> parse two tables:
        - by passenger TYPE   (Domestic / International / Transit / Total)
        - by COUNTRY          (Argentina / Brazil / Uruguay / Ecuador / Armenia / Italy)
     each with current-month, prior-year, YoY%, and YTD figures (units: thousands)
  4. validate, then atomically write data/traffic.json

Resilience: if anything fails (EDGAR down, format changed, validation fails) we
log and leave the existing traffic.json untouched, so the dashboard keeps the
last-known values. We never overwrite good data with a bad parse.
"""

import re
import sys
import time

from common import (
    extract_table_rows,
    http_get_json,
    http_get_text,
    now_iso,
    read_existing,
    to_number,
    write_json,
)

CIK = 1717393
CIK10 = str(CIK).zfill(10)
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"

# Canonical country set the release reports. Keys are matched against the first
# cell of each table row (case-insensitive, exact token).
COUNTRIES = ["Argentina", "Brazil", "Uruguay", "Ecuador", "Armenia", "Italy"]

TYPE_LABELS = {
    "domestic passengers": "Domestic",
    "international passengers": "International",
    "transit passengers": "Transit",
    "total passengers": "Total",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


def find_latest_traffic_release(max_filings=30):
    """Return (exhibit_url, html) for the newest 6-K that is a traffic release."""
    sub = http_get_json(SUBMISSIONS_URL)
    recent = sub["filings"]["recent"]
    forms = recent["form"]
    accs = recent["accessionNumber"]
    primary = recent.get("primaryDocument", [""] * len(forms))

    scanned = 0
    for form, acc, prim in zip(forms, accs, primary):
        if not form.startswith("6-K"):
            continue
        if scanned >= max_filings:
            break
        scanned += 1
        accnodash = acc.replace("-", "")
        base = ARCHIVE.format(cik=CIK, acc=accnodash)
        try:
            idx = http_get_json(base + "/index.json")
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {acc}: index.json failed ({exc})")
            continue
        names = [it["name"] for it in idx["directory"]["item"]]
        # Prefer EX-99.* exhibits; fall back to the primary 6-K document.
        candidates = [n for n in names
                      if n.lower().endswith((".htm", ".html"))
                      and re.search(r"ex-?99", n, re.I)]
        if prim and prim not in candidates:
            candidates.append(prim)
        for name in candidates:
            try:
                html = http_get_text(base + "/" + name)
            except Exception as exc:  # noqa: BLE001
                print(f"  skip {name}: fetch failed ({exc})")
                continue
            if "Passenger Traffic" in html and "International Passengers" in html:
                return base + "/" + name, html
        time.sleep(0.3)  # be polite to EDGAR between filings
    raise RuntimeError("no traffic 6-K found in recent filings")


def detect_period(rows, html):
    """Return dict {label, month, year, current_col, prior_col, has_ytd}."""
    # 1) Try the column header tokens like "Apr'26" / "Apr'25".
    col_token = re.compile(r"^([A-Za-z]{3})'(\d{2})$")
    current_col = prior_col = None
    has_ytd = False
    for row in rows:
        toks = [c.strip() for c in row]
        if any(t.upper().startswith("YTD") for t in toks):
            has_ytd = True
        matches = [t for t in toks if col_token.match(t)]
        if len(matches) >= 2 and current_col is None:
            current_col, prior_col = matches[0], matches[1]
    month = year = None
    if current_col:
        m = col_token.match(current_col)
        month = MONTHS.get(m.group(1).lower())
        year = 2000 + int(m.group(2))
    # 2) Fall back to the headline in the release body.
    if month is None or year is None:
        m = re.search(r"Reports\s+([A-Za-z]+)\s+(\d{4})\s+Passenger Traffic", html)
        if m:
            month = list(MONTHS.values())[
                [k[:3] for k in MONTHS].index(m.group(1)[:3].lower())
            ] if m.group(1)[:3].lower() in MONTHS else None
            month = MONTHS.get(m.group(1)[:3].lower())
            year = int(m.group(2))
    label = f"{MONTH_NAMES[month]} {year}" if month and year else (current_col or "Latest")
    return {
        "label": label,
        "month": month,
        "year": year,
        "current_col": current_col,
        "prior_col": prior_col,
        "has_ytd": has_ytd,
    }


def parse_row_values(cells):
    """Map a data row's cells to current/prior/yoy (+ YTD) numbers.

    Drops empty spacer cells, then maps positionally:
      [current, prior, yoy%]  or  [current, prior, yoy%, ytd_cur, ytd_prior, ytd%]
    """
    vals = [c for c in cells[1:] if c.strip() not in ("", "\xa0")]
    rec = {
        "current": None, "prior": None, "yoy_pct": None,
        "ytd_current": None, "ytd_prior": None, "ytd_yoy_pct": None,
    }
    if len(vals) >= 3:
        rec["current"] = to_number(vals[0])
        rec["prior"] = to_number(vals[1])
        rec["yoy_pct"] = to_number(vals[2])
    if len(vals) >= 6:
        rec["ytd_current"] = to_number(vals[3])
        rec["ytd_prior"] = to_number(vals[4])
        rec["ytd_yoy_pct"] = to_number(vals[5])
    return rec


def parse_by_type(rows):
    out = {}
    for row in rows:
        if not row:
            continue
        label = row[0].lower()
        label = re.sub(r"\(.*?\)", "", label).strip()  # drop "(thousands)"
        for key, name in TYPE_LABELS.items():
            if label.startswith(key):
                rec = parse_row_values(row)
                if rec["current"] is not None:
                    out[name] = rec
    return out


def parse_by_country(rows, total_current):
    """First-occurrence country rows form the passenger-by-country table.

    The by-country passenger table appears before any cargo/movements tables, so
    the first row matching each country name is its passenger row. We validate by
    checking the six countries sum to the reported total.
    """
    found = {}
    for row in rows:
        if not row:
            continue
        first = re.sub(r"\(.*?\)", "", row[0]).strip()
        for country in COUNTRIES:
            if country in found:
                continue
            if first.lower() == country.lower():
                rec = parse_row_values(row)
                if rec["current"] is not None:
                    found[country] = rec
    ordered = [{"name": c, **found[c]} for c in COUNTRIES if c in found]
    ordered.sort(key=lambda r: r["current"], reverse=True)
    return ordered


def build_payload():
    url, html = find_latest_traffic_release()
    rows = extract_table_rows(html)
    period = detect_period(rows, html)
    by_type = parse_by_type(rows)
    if not {"Domestic", "International", "Total"}.issubset(by_type):
        raise RuntimeError(f"by-type parse incomplete: got {sorted(by_type)}")

    total_rec = by_type["Total"]
    by_country = parse_by_country(rows, total_rec["current"])
    if len(by_country) < 5:
        raise RuntimeError(f"by-country parse incomplete: got {[c['name'] for c in by_country]}")

    # Sanity: the six countries should sum to the reported total (within 2%).
    csum = sum(c["current"] for c in by_country)
    if total_rec["current"] and abs(csum - total_rec["current"]) / total_rec["current"] > 0.02:
        raise RuntimeError(f"country sum {csum} != total {total_rec['current']}")

    intl = by_type["International"]
    dom = by_type["Domestic"]

    payload = {
        "updated_at": now_iso(),
        "status": "ok",
        "source": "SEC EDGAR 6-K EX-99.1 (CIK 1717393)",
        "source_url": url,
        "period": period,
        "units": "thousands",
        "headline": {"metric": "International Passengers", **intl},
        "secondary": {"metric": "Domestic Passengers", **dom},
        "total": total_rec,
        "by_type": [{"name": n, **by_type[n]} for n in ("Domestic", "International", "Transit") if n in by_type],
        "by_country": by_country,
    }
    return payload


def main():
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001
        prev = read_existing("traffic.json")
        if prev:
            print(f"[traffic] fetch/parse failed ({exc}); keeping last-known "
                  f"({prev.get('period', {}).get('label')}, updated {prev.get('updated_at')})")
        else:
            print(f"[traffic] fetch/parse failed ({exc}); no prior data to keep")
        return 0  # resilient by design: never break the pipeline
    wrote = write_json("traffic.json", payload)
    p = payload["period"]["label"]
    h = payload["headline"]
    summary = (f"International {h['current']:,.0f}k ({h['yoy_pct']:+}% YoY), "
               f"{len(payload['by_country'])} countries")
    if wrote is None:
        print(f"[traffic] {p} unchanged; kept existing file ({summary})")
    else:
        print(f"[traffic] wrote {p}: {summary}, updated {payload['updated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
