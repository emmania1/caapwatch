"""
Shared helpers for CAAPWATCH fetch scripts.

Design goals (read before editing):
  * STDLIB ONLY. No third-party packages, so the GitHub Action never needs a
    `pip install` step and can never break on a dependency. urllib + html.parser
    + xml + json cover every feed we use.
  * RESILIENT WRITES. A fetch script computes its new payload first and only
    writes the JSON file if that payload is valid. On any failure it leaves the
    existing file untouched, so the dashboard keeps showing the last-known good
    data with its (now older) "updated_at" timestamp. We never overwrite good
    data with garbage, and never leave a half-written file (atomic os.replace).
"""

import email.utils
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

# SEC and most sites want a descriptive User-Agent with contact info.
USER_AGENT = "CAAPWATCH/1.0 (static dashboard; contact en@pillsburycap.com)"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def now_iso():
    """UTC timestamp, second precision, e.g. 2026-06-01T12:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url, *, timeout=40, retries=3, accept=None):
    """GET a URL as bytes with a real User-Agent and simple backoff.

    Raises the last exception if every attempt fails — callers are expected to
    catch and fall back to last-known data.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if accept:
        headers["Accept"] = accept
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - we intentionally retry everything
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last


def http_get_text(url, *, timeout=40, retries=3, accept=None, encoding="utf-8"):
    return http_get(url, timeout=timeout, retries=retries, accept=accept).decode(encoding, "replace")


def http_get_json(url, *, timeout=40, retries=3):
    return json.loads(http_get_text(url, timeout=timeout, retries=retries, accept="application/json"))


def data_path(name):
    return os.path.join(DATA_DIR, name)


def read_existing(name):
    """Return the currently stored JSON for a data file, or None."""
    try:
        with open(data_path(name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def write_json(name, payload, skip_if_unchanged=True):
    """Atomically write payload (a dict) to data/<name>.

    Stamps `updated_at`/`status` if absent. Writes to a temp file in the same
    dir then os.replace() so readers never see a partial file.

    When skip_if_unchanged is True, we compare against the existing file
    ignoring `updated_at`; if the substantive data is identical we skip the
    write entirely and return None. That keeps `updated_at` meaningful (it marks
    when the data last *changed*) and stops the daily Action from committing
    no-op churn. Returns the written path, or None if skipped.
    """
    payload.setdefault("updated_at", now_iso())
    payload.setdefault("status", "ok")
    if skip_if_unchanged:
        prev = read_existing(name)
        if prev is not None:
            a = {k: v for k, v in prev.items() if k != "updated_at"}
            b = {k: v for k, v in payload.items() if k != "updated_at"}
            if a == b:
                return None
    os.makedirs(DATA_DIR, exist_ok=True)
    target = data_path(name)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def to_number(text):
    """Parse a financial-table cell into a float, or None.

    Handles thousands separators ("3,405"), percentages ("-3.5%"), parenthesised
    negatives ("(120)"), and en/em dashes used for "no value".
    """
    if text is None:
        return None
    s = text.strip().replace(" ", "").replace("\xa0", " ").strip()
    if s in ("", "-", "–", "—", "—", "–", "n/a", "N/A", "nm", "NM"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("%", "").replace(",", "").replace("+", "").strip()
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


class _RowExtractor(HTMLParser):
    """Collect every table row in a document as a list of cell-text strings.

    We deliberately ignore table grouping and nesting: financial press-release
    exhibits sometimes nest layout tables, but each *data* row is a flat <tr>
    with <td> cells. Matching rows by their first cell (the row label) is far
    more robust than relying on table indices that shift between filings.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def close(self):
        super().close()
        if self._row:
            self.rows.append(self._row)
            self._row = None


def extract_table_rows(html):
    """Return a list of rows; each row is a list of cell-text strings."""
    p = _RowExtractor()
    p.feed(html)
    p.close()
    return p.rows


# ---------------------------------------------------------------------------
# Google News RSS — shared by the concession, fuel and armenia feeds.
# Free, no key. We build a search RSS URL, fetch it, and parse <item>s with
# the stdlib XML parser. pubDate is RFC-822; we normalise it to ISO-UTC.
# ---------------------------------------------------------------------------
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def google_news_url(query, hl="en-US", gl="US", ceid="US:en"):
    """Build a Google News RSS search URL. Locale params let the concession and
    armenia feeds pull Brazilian-Portuguese / Argentine-Spanish coverage."""
    return GOOGLE_NEWS_RSS.format(q=urllib.parse.quote(query), hl=hl, gl=gl, ceid=ceid)


def _rfc822_to_iso(value):
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def parse_rss(xml_text, max_items=20):
    """Parse an RSS feed into a list of {title, link, source, published_iso}."""
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if not title:
            continue
        items.append({
            "title": title,
            "link": link,
            "source": source,
            "published_iso": _rfc822_to_iso(pub),
        })
        if len(items) >= max_items:
            break
    return items


def fetch_news(query, max_items=20, hl="en-US", gl="US", ceid="US:en"):
    """Run a Google News RSS search and return parsed items (best-effort)."""
    xml_text = http_get_text(google_news_url(query, hl, gl, ceid), accept="application/rss+xml")
    return parse_rss(xml_text, max_items=max_items)
