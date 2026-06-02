# CAAPWATCH

A static demand-watch dashboard for **Corporación América Airports** (NYSE:
**CAAP**) — the operator of 52 airports across Argentina, Brazil, Uruguay,
Ecuador, Armenia and Italy. It answers one question at a glance:

> **Is the traffic & concession story holding up across these markets?**

It is a plain static site (HTML + CSS + vanilla JS). There is **no framework, no
build step, and no server**. It is designed to be hosted on **GitHub Pages** and
to load instantly and reliably every time.

---

## How it works (architecture)

Data fetching is fully **decoupled** from serving:

```
            ┌─────────────────────────┐         ┌──────────────────────┐
            │ GitHub Action (daily)    │         │ GitHub Pages         │
            │  scripts/fetch_*.py      │  commits │  serves static files │
            │  → writes /data/*.json   │ ───────► │  index.html + /data  │
            └─────────────────────────┘   JSON   └──────────────────────┘
                       │                                     │
                  the ONLY thing                    the browser only ever
                  that hits the network             reads pre-fetched JSON
```

* **The page never calls an API live in the browser.** `js/app.js` only `fetch()`s
  the local JSON files in `/data`. So there is nothing to spin down, time out, or
  rate-limit at view time — if GitHub Pages is up, the dashboard renders.
* **A single GitHub Action** (`.github/workflows/refresh.yml`) runs the Python
  fetchers on a daily cron, commits any changed `/data/*.json` back to the repo,
  then stops.
* **Every panel degrades gracefully.** If a data file is missing or a fetch
  failed, that panel shows its last-known values with an "Updated …" timestamp,
  or a quiet "no data yet" box — it never throws, and one broken feed can never
  blank the page.

The Python fetchers use the **standard library only** (no `pip install`), so the
refresh job has no dependencies that could break.

---

## Repository layout

```
caapwatch/
├── index.html                 # the dashboard shell (panels are populated by JS)
├── css/style.css              # all styling, plain CSS
├── js/app.js                  # resilient render framework + panel renderers
├── data/                      # pre-fetched JSON the page reads (committed by the Action)
│   ├── traffic.json           #   Panel 1 — auto (SEC EDGAR)
│   ├── gdp.json               #   Panel 2 — auto (World Bank)
│   ├── concession.json        #   Panel 3 — auto headlines (Google News)
│   ├── fuel.json              #   Panel 4 — auto (Stooq oil + Google News)
│   ├── armenia.json           #   Panel 5 — auto headlines (Google News)
│   ├── price.json             #   Header chip — auto CAAP share price (Stooq)
│   └── status.json            #   Panels 3 & 5 — HAND-EDITED manual fields
├── scripts/                   # the fetchers — the only code that hits the network
│   ├── common.py              #   shared helpers (HTTP, RSS, atomic resilient writes, table parsing)
│   ├── fetch_traffic.py       #   Panel 1 — SEC EDGAR 6-K
│   ├── fetch_gdp.py           #   Panel 2 — World Bank GDP + ARG inflation/FX
│   ├── fetch_concession.py    #   Panel 3 — Brasília (pt-BR) + AA2000 (es-419) news
│   ├── fetch_fuel.py          #   Panel 4 — Stooq oil + airline-capacity news + risk
│   ├── fetch_armenia.py       #   Panel 5 — Armenia peace-process news
│   ├── fetch_price.py         #   Header chip — CAAP share price (Stooq)
│   └── requirements.txt       #   (documents that there are no dependencies)
└── .github/workflows/refresh.yml   # the daily refresh + commit Action
```

---

## The panels

| # | Panel | Data | Source | Status |
|---|-------|------|--------|--------|
| 1 | **Traffic by Market** (hero) | auto | CAAP monthly traffic release, via **SEC EDGAR** 6-K `EX-99.1` (CIK 1717393) | ✅ live |
| 2 | GDP Growth (+ Argentina inflation/FX) | auto | World Bank API (`NY.GDP.MKTP.KD.ZG`, `FP.CPI.TOTL.ZG`, `PA.NUS.FCRF`; ARG/BRA/ITA/ARM/URY/ECU) | ✅ live |
| 3 | Concession Tracker | feed + manual | Google News RSS (pt-BR + es-419); stage in `status.json` | ✅ live |
| 4 | Fuel / Capacity Risk (ribbon) | auto + feed | Stooq oil prices (Brent/WTI/jet-proxy) + Google News RSS (airline capacity) | ✅ live |
| 5 | Armenia Political Watch | feed + manual | Google News RSS; tension level in `status.json` | ✅ live |
| — | Share-price chip (header) | auto | Stooq live quote (`caap.us`); self-built price history | ✅ live |

**Panel 1 note.** CAAP furnishes each monthly passenger-traffic press release to
the SEC as a 6-K. The release text (identical numbers to the PDF on the IR site)
lives in the filing's `EX-99.1` exhibit as clean HTML. We read it through
EDGAR's free JSON API — no key, no bot-challenge, no PDF parsing — which is far
more reliable to run unattended than scraping the JavaScript IR site. The fetcher
extracts per-country totals and the domestic / international / transit split with
YoY and YTD figures, in thousands of passengers.

---

## Enabling GitHub Pages

1. Create a GitHub repo and push this folder to it:
   ```bash
   git add -A
   git commit -m "Initial CAAPWATCH"
   git branch -M main
   git remote add origin https://github.com/<you>/caapwatch.git
   git push -u origin main
   ```
2. In the repo: **Settings → Pages**.
3. Under **Build and deployment → Source**, choose **Deploy from a branch**.
4. Set **Branch** to `main` and **folder** to `/ (root)`, then **Save**.
5. After a minute the site is live at `https://<you>.github.io/caapwatch/`.

That's it — because the site is plain static files at the repo root, no build is
involved.

---

## How the refresh Action works

`.github/workflows/refresh.yml`:

* Runs on a **daily cron** (`0 11 * * *`, 11:00 UTC) and can also be triggered by
  hand from the repo's **Actions** tab (**Run workflow**).
* Runs each `scripts/fetch_*.py` that exists. Scripts not built yet are skipped,
  so the workflow is safe to keep enabled as panels are added.
* Each fetcher writes its JSON **only when the data actually changed**, so the
  Action commits at most a small diff and only when there is news — no daily
  no-op churn.
* Commits any changed `data/*.json` back to the repo (as `caapwatch-bot`) and
  pushes. GitHub Pages then redeploys automatically.

It needs no secrets: the built-in `GITHUB_TOKEN` plus `permissions: contents:
write` (already set in the workflow) is enough to commit back.

**To change the schedule**, edit the `cron:` line. **To pause refreshes**, disable
the workflow in the Actions tab.

---

## Editing `data/status.json` by hand

`data/status.json` holds the **manual judgement fields** that the auto-fetchers
must never overwrite — the concession stage and the Armenia tension level. Edit
it directly, commit, and the dashboard updates on next deploy.

```jsonc
{
  "updated_at": "2026-06-01",            // bump this when you change anything

  "concession": {
    "brasilia": {
      "stage": "docs pending",           // ← set this to one of the values in "stages"
      "stages": ["docs pending", "auction dated", "auction held", "result"],
      "note": "…free-text context…",
      "expected": "auction by end-2026"
    },
    "aa2000": {
      "stage": "rebalancing in negotiation",
      "stages": ["stable", "rebalancing in negotiation", "agreement reached", "buyout exercised"],
      "note": "…",
      "expected": "ongoing"
    }
  },

  "armenia": {
    "tension_level": "elevated",         // ← set this to one of the values in "levels"
    "levels": ["calm", "watch", "elevated", "high", "critical"],
    "note": "…",
    "as_of": "2026-06-01"
  }
}
```

Rules of thumb:

* Keep `stage` / `tension_level` spelled **exactly** as one of the items in the
  matching `stages` / `levels` list — that's what drives the highlighted step and
  the tension dots.
* The auto-fetched headlines (when wired) are written to *separate* files, so
  editing `status.json` never collides with the refresh job.

---

## Local preview

Any static file server works — no build:

```bash
cd caapwatch
python3 -m http.server 8011
# open http://localhost:8011
```

To refresh the data locally (each writes its JSON into `/data`):

```bash
python3 scripts/fetch_traffic.py      # Panel 1 — traffic
python3 scripts/fetch_gdp.py          # Panel 2 — GDP / inflation / FX
python3 scripts/fetch_concession.py   # Panel 3 — concession headlines
python3 scripts/fetch_fuel.py         # Panel 4 — oil + capacity headlines
python3 scripts/fetch_armenia.py      # Panel 5 — Armenia headlines
python3 scripts/fetch_price.py        # Header chip — CAAP share price
```

Each is independent and resilient — if one fails it leaves its last-known JSON
in place, so you can run them individually or all together.

---

## Disclaimer

CAAPWATCH aggregates public data for monitoring purposes only. It is **not
investment advice**.
