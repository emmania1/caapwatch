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


def find_latest_traffic_release(sub, max_filings=30):
    """Return (exhibit_url, html) for the newest 6-K that is a traffic release."""
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


# ---------------------------------------------------------------------------
# Quarterly per-country INTERNATIONAL split.
#
# The monthly release reports only the network-wide domestic/international/transit
# split plus per-country TOTALS. International passengers BY COUNTRY appear only
# in the QUARTERLY earnings release — a separate 6-K whose reportDate is a
# quarter-end — inside a consolidated "operating statistics" block that lists, per
# country, Domestic / International / Transit / Total passengers IN MILLIONS
# (one decimal; "n.m." for negligible) under a `1Q26 | 1Q25 | % Var.` header
# (a Q1 report has no separate YTD column — the quarter is the YTD).
#
# We fetch that release separately and parse ONLY the consolidated block, which
# we isolate by anchoring on bare country-header rows (first cell a country, the
# rest blank) that are immediately followed by an "International Passengers" row.
# That uniquely selects the consolidated passenger table and skips both the
# per-segment operating blocks (no bare header) and the later revenue blocks (not
# followed by an intl-pax row). The per-country figures must reconcile with the
# SAME filing's network International total (reported in THOUSANDS in the top
# table) or we reject the parse and keep last-known. This block is QUARTERLY and
# coarser (0.1M) than the monthly hero; a failure here never breaks the hero.
# ---------------------------------------------------------------------------
QUARTER_END = {"03-31", "06-30", "09-30", "12-31"}
QCOL = re.compile(r"^\d[Qq]\d{2}$")


def find_latest_quarterly_release(sub, max_filings=40):
    """Return (url, html, report_date) for the newest 6-K earnings release that
    carries the consolidated per-country operating-statistics block.

    Quarterly releases have a quarter-end reportDate (monthly traffic releases do
    not), so we narrow to quarter-end-dated 6-Ks and then confirm by content.
    Submissions are newest-first, so the first content match is the latest quarter.
    """
    recent = sub["filings"]["recent"]
    forms = recent["form"]
    accs = recent["accessionNumber"]
    primary = recent.get("primaryDocument", [""] * len(forms))
    rdates = recent.get("reportDate", [""] * len(forms))

    scanned = 0
    for form, acc, prim, rdate in zip(forms, accs, primary, rdates):
        if not form.startswith("6-K"):
            continue
        if (rdate or "")[5:] not in QUARTER_END:   # quarter-end reportDate only
            continue
        if scanned >= max_filings:
            break
        scanned += 1
        accnodash = acc.replace("-", "")
        base = ARCHIVE.format(cik=CIK, acc=accnodash)
        try:
            idx = http_get_json(base + "/index.json")
        except Exception as exc:  # noqa: BLE001
            print(f"  skip quarterly {acc}: index.json failed ({exc})")
            continue
        names = [it["name"] for it in idx["directory"]["item"]]
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
            # Quarterly earnings release marker: per-country split reported in millions.
            if ("International Passengers" in html
                    and "in millions" in html and "Var" in html):
                return base + "/" + name, html, rdate
        time.sleep(0.3)  # be polite to EDGAR between filings
    raise RuntimeError("no quarterly per-country release found in recent filings")


def _clean_label(text):
    return re.sub(r"\(.*?\)", "", text or "").strip()


def _country_header(row):
    """If row is a bare country-header row (first cell a country, rest blank),
    return the canonical country name; else None."""
    if not row:
        return None
    name = _clean_label(row[0])
    for c in COUNTRIES:
        if name.lower() == c.lower() and all((x or "").strip() == "" for x in row[1:4]):
            return c
    return None


def parse_intl_by_market(rows):
    """Parse the consolidated per-country block of a quarterly EX-99.1.

    Returns (markets, period_label, prior_label). Each market dict carries
    international (current/prior/yoy%) plus domestic/transit/total CURRENT levels,
    in millions of passengers exactly as the filing reports them (no derivation).
    """
    n = len(rows)

    def intl_follows(i, k=4):
        for j in range(i + 1, min(i + 1 + k, n)):
            lab = _clean_label(rows[j][0]).lower() if rows[j] else ""
            if lab.startswith("international passenger"):
                return True
            if _country_header(rows[j]):
                return False
        return False

    # Passenger consolidated block = bare country headers each followed by an
    # International Passengers row.
    headers = [(i, _country_header(rows[i])) for i in range(n)
               if _country_header(rows[i]) and intl_follows(i)]
    if not headers:
        return [], None, None

    # Period columns from the header row just above the first country header.
    first = headers[0][0]
    period_label = prior_label = None
    for j in range(first - 1, max(first - 6, -1), -1):
        toks = [c.strip() for c in rows[j]]
        qs = [t for t in toks if QCOL.match(t)]
        if len(qs) >= 2:
            period_label, prior_label = qs[0].upper(), qs[1].upper()
            break

    hdr_idxs = [i for i, _ in headers]
    markets = []
    for k, (i, country) in enumerate(headers):
        stop = hdr_idxs[k + 1] if k + 1 < len(hdr_idxs) else min(i + 8, n)
        rec = {"name": country, "intl_current": None, "intl_prior": None,
               "intl_yoy_pct": None, "dom_current": None,
               "transit_current": None, "total_current": None}
        for j in range(i + 1, stop):
            row = rows[j]
            if not row:
                continue
            lab = _clean_label(row[0]).lower()
            vals = parse_row_values(row)
            if lab.startswith("international passenger"):
                rec["intl_current"] = vals["current"]
                rec["intl_prior"] = vals["prior"]
                rec["intl_yoy_pct"] = vals["yoy_pct"]
            elif lab.startswith("domestic passenger"):
                rec["dom_current"] = vals["current"]
            elif lab.startswith("transit passenger"):
                rec["transit_current"] = vals["current"]
            elif lab.startswith("total passenger"):
                rec["total_current"] = vals["current"]
        markets.append(rec)
    return markets, period_label, prior_label


def network_intl_thousands(rows):
    """Network International total (in thousands) from the quarterly top table —
    the first 'International Passengers' row whose label says 'thousand'."""
    for row in rows:
        if not row:
            continue
        lab = row[0].lower()
        if lab.startswith("international passengers") and "thousand" in lab:
            return parse_row_values(row)["current"]
    return None


def _quarter_label_from_date(iso):
    """Map a quarter-end report date to a human label: '2026-03-31' -> 'Q1 2026'.
    Derived from the release date so the panel always names the actual quarter."""
    try:
        y, m, _ = str(iso).split("-")
        q = (int(m) - 1) // 3 + 1
        return f"Q{q} {int(y)}"
    except Exception:
        return None


def build_intl_by_market(sub):
    """Fetch the latest quarterly release and return the per-country international
    block, reconciled against that filing's network total.

    Raises on incompleteness or a failed reconciliation so the caller keeps
    last-known rather than publishing a bad split.
    """
    url, html, rdate = find_latest_quarterly_release(sub)
    rows = extract_table_rows(html)
    markets, period_label, prior_label = parse_intl_by_market(rows)
    markets = [m for m in markets if m["intl_current"] is not None]
    if len(markets) < 5:
        raise RuntimeError(f"intl-by-market parse incomplete: {[m['name'] for m in markets]}")

    net_k = network_intl_thousands(rows)
    intl_sum = round(sum(m["intl_current"] for m in markets), 1)
    recon_ok = None
    if net_k:
        net_m = net_k / 1000.0
        recon_ok = abs(intl_sum - net_m) / net_m <= 0.05
        if not recon_ok:
            raise RuntimeError(
                f"intl-by-market reconciliation failed: per-country sum {intl_sum}M "
                f"vs network {net_m:.3f}M")

    markets.sort(key=lambda m: m["intl_current"], reverse=True)
    # Name the quarter from the release date (e.g. "Q1 2026"); fall back to the
    # raw column token only if the date is unparseable.
    period_label = _quarter_label_from_date(rdate) or period_label
    return {
        "source_url": url,
        "report_date": rdate,
        "period_label": period_label,
        "prior_label": prior_label,
        "units": "millions",
        "metric": "International Passengers",
        "markets": markets,
        "reconciliation": {
            "intl_sum_millions": intl_sum,
            "network_intl_thousands": net_k,
            "ok": recon_ok,
        },
    }


def build_payload():
    sub = http_get_json(SUBMISSIONS_URL)
    url, html = find_latest_traffic_release(sub)
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

    # Quarterly per-country international split (separate filing, coarser cadence
    # and precision). Resilient: never let this break the monthly hero — on any
    # failure we carry the last-known quarterly block, or omit it on a cold start.
    try:
        payload["intl_by_market"] = build_intl_by_market(sub)
    except Exception as exc:  # noqa: BLE001
        prev = read_existing("traffic.json") or {}
        if prev.get("intl_by_market"):
            payload["intl_by_market"] = prev["intl_by_market"]
            print(f"[traffic] intl-by-market refresh failed ({exc}); kept last-known quarterly block")
        else:
            print(f"[traffic] intl-by-market unavailable ({exc}); omitting")
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
    ibm = payload.get("intl_by_market")
    ibm_note = (f", intl-by-market {len(ibm['markets'])}@{ibm['period_label']} "
                f"(recon {'ok' if ibm.get('reconciliation', {}).get('ok') else 'check'})") if ibm else ""
    summary = (f"International {h['current']:,.0f}k ({h['yoy_pct']:+}% YoY), "
               f"{len(payload['by_country'])} countries{ibm_note}")
    if wrote is None:
        print(f"[traffic] {p} unchanged; kept existing file ({summary})")
    else:
        print(f"[traffic] wrote {p}: {summary}, updated {payload['updated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
