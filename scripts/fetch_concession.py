#!/usr/bin/env python3
"""
Panel 3 — Concession Tracker news feed.

Pulls headlines for CAAP's two live concession catalysts from Google News RSS
(free, no key), in the local language where that matters:

  * brasilia — the Brasília (BSB / "JK") airport re-concession. Brazilian press
    covers this in Portuguese, so we query pt-BR (leilão / concessão / ANAC /
    Inframerica).
  * aa2000 — CAAP's Argentine concession (runs to 2038). The story is the
    economic-equilibrium rebalancing with the regulator ORSNA, the tariff /
    "ajuste aeroportuario" policy fight, and the concession capex plan, covered
    in Argentine Spanish (es-419). The operator rebranded in 2024 from
    "Aeropuertos Argentina 2000" to plain "Aeropuertos Argentina", so a query
    locked to the old name goes stale — we search both names plus ORSNA and
    merge several queries (see TOPICS), keeping only the last ~180 days so the
    card never shows year-old headlines.

This feed is intentionally SEPARATE from data/status.json. status.json holds the
hand-set "stage" judgement for each catalyst (which a human curates); this file
holds only auto-fetched headlines. The fetcher never touches status.json, so the
news refresh can never clobber the manual call (per the resilience contract).

Each kept headline is tagged with its topic so the dashboard can file it under
the right sub-card. On a failed fetch we keep the last-known headlines.
"""

import sys
from datetime import datetime, timedelta, timezone

from common import fetch_news, now_iso, read_existing, write_json

# topic -> (queries, locale tuple hl/gl/ceid, relevance keywords).
# Each topic merges SEVERAL queries so coverage doesn't hinge on one phrasing;
# results are then filtered to MAX_AGE_DAYS and sorted newest-first.
TOPICS = {
    "brasilia": {
        "label": "Brasília / JK re-concession",
        "queries": [
            '(leilão OR concessão OR reconcessão OR auction) aeroporto Brasília (Inframerica OR ANAC OR concessionária)',
        ],
        "locale": ("pt-BR", "BR", "BR:pt"),
        "keywords": ["brasília", "brasilia", "aeroporto", "concess", "leilão", "leilao",
                     "anac", "inframerica", "bsb", "airport", "auction", "concession"],
    },
    "aa2000": {
        "label": "AA2000 / Aeropuertos Argentina",
        # Search BOTH the legacy "Aeropuertos Argentina 2000" and the post-2024
        # rebrand "Aeropuertos Argentina", plus the regulator (ORSNA) and the
        # live sub-stories: rebalancing, the tariff/"ajuste" fight, capex plan.
        "queries": [
            '"Aeropuertos Argentina 2000" (ORSNA OR concesión OR tarifas OR canon OR reequilibrio OR inversión)',
            '"Aeropuertos Argentina" (ORSNA OR concesión OR tarifas OR reequilibrio OR "revisión tarifaria")',
            'ORSNA (tarifas OR concesión OR aeroportuaria OR revisión OR ajuste)',
            '"Aeropuertos Argentina" (inversión OR ampliación OR obras OR modernización OR Eurnekian)',
            'AA2000 (ORSNA OR concesión OR tarifas)',
        ],
        "locale": ("es-419", "AR", "AR:es"),
        # Require an operator/regulator identifier so generic regional-airport
        # stories (Bariloche tax spats, Tucumán works) don't slip through.
        "keywords": ["aa2000", "aeropuertos argentina", "orsna", "corporación américa",
                     "corporacion america", "eurnekian"],
    },
}

MAX_PER_TOPIC = 5
MAX_AGE_DAYS = 180          # drop anything older than this so the feed stays current


def relevant(title, keywords):
    t = title.lower()
    return any(k in t for k in keywords)


def fetch_topic(spec, cutoff_iso):
    """Merge a topic's queries, keep only relevant headlines from the last
    MAX_AGE_DAYS, dedupe by title, and sort newest-first.

    Each query is fetched independently so one failing (or thin) query never
    sinks the topic — the rest still contribute.
    """
    hl, gl, ceid = spec["locale"]
    seen, kept = set(), []
    for query in spec["queries"]:
        try:
            items = fetch_news(query, max_items=14, hl=hl, gl=gl, ceid=ceid)
        except Exception as exc:  # noqa: BLE001 - skip the bad query, keep the others
            print(f"[fetch_concession]   query failed ({type(exc).__name__}); skipping one")
            continue
        for it in items:
            if not relevant(it["title"], spec["keywords"]):
                continue
            pub = it.get("published_iso") or ""
            if not pub or pub < cutoff_iso:      # last ~180 days only
                continue
            key = it["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            kept.append(it)
    kept.sort(key=lambda x: x.get("published_iso") or "", reverse=True)
    return kept[:MAX_PER_TOPIC]


def build_payload():
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = read_existing("concession.json") or {}
    prev_topics = prev.get("topics") or {}

    topics = {}
    total = 0
    errors = 0
    for name, spec in TOPICS.items():
        try:
            kept = fetch_topic(spec, cutoff_iso)
        except Exception as exc:  # noqa: BLE001 - one topic failing keeps the other
            print(f"[fetch_concession] topic {name} failed: {type(exc).__name__}")
            errors += 1
            kept = prev_topics.get(name, [])
        if not kept:
            # Empty after filtering (thin run or transient) — keep last-known
            # rather than blanking a previously-good card.
            carry = prev_topics.get(name, [])
            if carry:
                print(f"[fetch_concession] topic {name} empty this run — keeping {len(carry)} last-known")
            kept = carry
        topics[name] = kept
        total += len(kept)

    # If every topic failed AND we have no carry-over, signal failure to keep last-known.
    if errors == len(TOPICS) and total == 0:
        raise ValueError("all concession topics failed")

    return {
        "updated_at": now_iso(),
        "status": "ok",
        "source": "Google News RSS",
        "source_url": "https://news.google.com/search?q=Bras%C3%ADlia%20aeroporto%20concess%C3%A3o",
        "topics": topics,
    }


def main():
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001 - resilient: keep last-known on any failure
        print(f"[fetch_concession] FAILED ({type(exc).__name__}: {exc}); keeping last-known data")
        return 0
    result = write_json("concession.json", payload)
    counts = {k: len(v) for k, v in payload["topics"].items()}
    if result is None:
        print(f"[fetch_concession] no change — left existing untouched (counts {counts})")
    else:
        print(f"[fetch_concession] wrote concession.json (counts {counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
