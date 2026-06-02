#!/usr/bin/env python3
"""
Panel 3 — Concession Tracker news feed.

Pulls headlines for CAAP's two live concession catalysts from Google News RSS
(free, no key), in the local language where that matters:

  * brasilia — the Brasília (BSB / "JK") airport re-concession. Brazilian press
    covers this in Portuguese, so we query pt-BR (leilão / concessão / ANAC /
    Inframerica).
  * aa2000 — Aeropuertos Argentina 2000, CAAP's Argentine concession (runs to
    2038). The story is economic-equilibrium rebalancing with the regulator
    ORSNA, covered in Argentine Spanish (es-419).

This feed is intentionally SEPARATE from data/status.json. status.json holds the
hand-set "stage" judgement for each catalyst (which a human curates); this file
holds only auto-fetched headlines. The fetcher never touches status.json, so the
news refresh can never clobber the manual call (per the resilience contract).

Each kept headline is tagged with its topic so the dashboard can file it under
the right sub-card. On a failed fetch we keep the last-known headlines.
"""

import sys

from common import fetch_news, now_iso, read_existing, write_json

# topic -> (query, locale tuple hl/gl/ceid, relevance keywords)
TOPICS = {
    "brasilia": {
        "label": "Brasília / JK re-concession",
        "query": '(leilão OR concessão OR reconcessão OR auction) aeroporto Brasília (Inframerica OR ANAC OR concessionária)',
        "locale": ("pt-BR", "BR", "BR:pt"),
        "keywords": ["brasília", "brasilia", "aeroporto", "concess", "leilão", "leilao",
                     "anac", "inframerica", "bsb", "airport", "auction", "concession"],
    },
    "aa2000": {
        "label": "AA2000 (Argentina)",
        "query": '("Aeropuertos Argentina 2000" OR AA2000) (ORSNA OR concesión OR tarifas OR canon OR revisión)',
        "locale": ("es-419", "AR", "AR:es"),
        "keywords": ["aa2000", "aeropuertos argentina", "orsna", "concesi", "tarifa",
                     "canon", "aeropuerto", "corporación américa", "corporacion america"],
    },
}

MAX_PER_TOPIC = 5


def relevant(title, keywords):
    t = title.lower()
    return any(k in t for k in keywords)


def fetch_topic(spec):
    hl, gl, ceid = spec["locale"]
    items = fetch_news(spec["query"], max_items=14, hl=hl, gl=gl, ceid=ceid)
    seen, kept = set(), []
    for it in items:
        if not relevant(it["title"], spec["keywords"]):
            continue
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(it)
    kept.sort(key=lambda x: x.get("published_iso") or "", reverse=True)
    return kept[:MAX_PER_TOPIC]


def build_payload():
    topics = {}
    total = 0
    errors = 0
    for name, spec in TOPICS.items():
        try:
            topics[name] = fetch_topic(spec)
            total += len(topics[name])
        except Exception as exc:  # noqa: BLE001 - one topic failing keeps the other
            print(f"[fetch_concession] topic {name} failed: {type(exc).__name__}")
            errors += 1
            prev = read_existing("concession.json") or {}
            topics[name] = (prev.get("topics") or {}).get(name, [])

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
