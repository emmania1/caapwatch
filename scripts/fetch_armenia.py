#!/usr/bin/env python3
"""
Panel 5 — Armenia Political Watch news feed.

Pulls headlines on the Armenia–Azerbaijan peace process and its escalation
triggers from Google News RSS (free, no key). CAAP operates Zvartnots (Yerevan),
so the relevant watch-items are the unsigned peace treaty, the Zangezur / "TRIPP"
transit-corridor arrangements, Armenia's 2026 parliamentary elections, and
Russia/Iran involvement — any of which could swing Armenian traffic.

Like the concession feed, this is SEPARATE from data/status.json: the hand-set
"tension_level" judgement lives in status.json and is never touched here; this
file only carries auto-fetched headlines. On a failed fetch we keep last-known.
"""

import sys

from common import fetch_news, now_iso, read_existing, write_json

QUERIES = [
    'Armenia Azerbaijan peace (treaty OR deal OR agreement OR border)',
    '(Zangezur OR TRIPP) corridor Armenia Azerbaijan',
    'Armenia 2026 (election OR Pashinyan OR parliamentary OR vote)',
]

KEEP_TERMS = [
    "armenia", "armenian", "azerbaijan", "azeri", "pashinyan", "aliyev",
    "zangezur", "tripp", "corridor", "yerevan", "baku", "karabakh", "nagorno",
    "peace", "treaty", "border", "election", "syunik",
]

MAX_HEADLINES = 6


def relevant(title):
    t = title.lower()
    return any(k in t for k in KEEP_TERMS)


def build_payload():
    seen, kept = set(), []
    errors = 0
    for q in QUERIES:
        try:
            items = fetch_news(q, max_items=12)
        except Exception as exc:  # noqa: BLE001 - one query failing keeps the others
            print(f"[fetch_armenia] query failed: {type(exc).__name__}")
            errors += 1
            continue
        for it in items:
            if not relevant(it["title"]):
                continue
            key = it["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            kept.append(it)

    if errors == len(QUERIES):
        raise ValueError("all armenia queries failed")

    kept.sort(key=lambda x: x.get("published_iso") or "", reverse=True)

    # If filtering left nothing but we used to have headlines, keep the old ones
    # rather than blanking the feed.
    if not kept:
        prev = read_existing("armenia.json") or {}
        kept = list(prev.get("headlines") or [])

    return {
        "updated_at": now_iso(),
        "status": "ok",
        "source": "Google News RSS",
        "source_url": "https://news.google.com/search?q=Armenia%20Azerbaijan%20peace",
        "headlines": kept[:MAX_HEADLINES],
    }


def main():
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001 - resilient: keep last-known on any failure
        print(f"[fetch_armenia] FAILED ({type(exc).__name__}: {exc}); keeping last-known data")
        return 0
    result = write_json("armenia.json", payload)
    if result is None:
        print("[fetch_armenia] no change — left existing armenia.json untouched")
    else:
        print(f"[fetch_armenia] wrote armenia.json ({len(payload['headlines'])} headlines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
