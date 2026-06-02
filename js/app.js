/* CAAPWATCH — vanilla JS. No framework, no build.
 *
 * Resilience contract: every panel renders independently inside its own
 * try/catch. A missing or unparseable /data file yields a quiet "no data"
 * state — it never throws, and one broken feed can never blank the page.
 */

"use strict";

const DATA_FILES = {
  traffic: "data/traffic.json",
  gdp: "data/gdp.json",
  concession: "data/concession.json",
  fuel: "data/fuel.json",
  armenia: "data/armenia.json",
  status: "data/status.json",
};

const FLAGS = {
  Argentina: "🇦🇷", Brazil: "🇧🇷", Uruguay: "🇺🇾",
  Ecuador: "🇪🇨", Armenia: "🇦🇲", Italy: "🇮🇹",
};

/* ---------- data loading ---------- */
async function loadJSON(path) {
  try {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch (_) {
    return null;
  }
}

/* ---------- formatting helpers ---------- */
// Traffic figures are reported in THOUSANDS of passengers; show as millions.
function fmtM(thousands) {
  if (thousands == null || isNaN(thousands)) return "–";
  return (thousands / 1000).toFixed(2) + "M";
}
function fmtPct(n) {
  if (n == null || isNaN(n)) return "";
  return (n > 0 ? "+" : "") + Number(n).toFixed(1) + "%";
}
function pctClass(n) {
  if (n == null || isNaN(n)) return "flat";
  return n > 0 ? "pos" : n < 0 ? "neg" : "flat";
}
function pill(n) {
  return `<span class="pill ${pctClass(n)}">${fmtPct(n)}</span>`;
}
// Fuel pill: a price RISE is a headwind for traffic, so colour it red (and a
// fall green) — the inverse of pctClass, with a direction arrow.
function fuelPill(n) {
  if (n == null || isNaN(n)) return "";
  const cls = n > 0 ? "neg" : n < 0 ? "pos" : "flat";
  const arrow = n > 0 ? "▲" : n < 0 ? "▼" : "·";
  return `<span class="pill ${cls}">${arrow} ${Math.abs(Number(n)).toFixed(1)}%</span>`;
}
// Google News titles end in " - Publisher"; drop that for a cleaner headline.
// The second replace trims any dangling dash left by odd publisher names.
function cleanTitle(t) {
  return String(t || "").replace(/\s+[-–]\s+[^-–]+$/, "").replace(/\s*[-–]\s*$/, "").trim();
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function relTime(iso) {
  if (!iso) return null;
  const t = new Date(iso);
  if (isNaN(t)) return null;
  const days = Math.floor((Date.now() - t.getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return days + " days ago";
  return t.toISOString().slice(0, 10);
}
// Renders an "Updated …" stamp; flags stale (amber) past `maxDays`.
function stamp(iso, maxDays) {
  const rel = relTime(iso);
  if (!rel) return `<span class="stamp">no timestamp</span>`;
  const t = new Date(iso);
  const days = (Date.now() - t.getTime()) / 86400000;
  const stale = maxDays && days > maxDays;
  return `<span class="stamp${stale ? " stale" : ""}">Updated ${rel}${stale ? " · stale" : ""}</span>`;
}
function srcLink(url, label) {
  if (!url) return "";
  return `<a class="src-link" href="${esc(url)}" target="_blank" rel="noopener">${esc(label || "source")} ↗</a>`;
}
function nodata(msg) {
  return `<div class="nodata">${esc(msg || "No data yet — awaiting first refresh.")}</div>`;
}
// Shared headline list for the concession & armenia feeds.
function headlinesList(items, emptyMsg) {
  if (!items || !items.length) {
    return `<ul class="headlines"><li><span class="src">${esc(emptyMsg || "No recent headlines.")}</span></li></ul>`;
  }
  return `<ul class="headlines">` + items.map((h) => `
    <li><a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(cleanTitle(h.title))}</a>
    <span class="src">${esc(h.source || "")}${h.published_iso ? " · " + esc(relTime(h.published_iso)) : ""}</span></li>`).join("") + `</ul>`;
}
// Inline SVG sparkline from a [{year,value}] series. Stretches to fill width;
// a non-scaling stroke keeps the line crisp. Draws a dashed zero baseline when
// the series crosses zero (so negative GDP years read correctly).
function sparkline(series, color) {
  const pts = (series || []).map((p) => p.value).filter((v) => v != null && !isNaN(v));
  if (pts.length < 2) return "";
  const W = 100, H = 30, pad = 2.5;
  let min = Math.min(...pts), max = Math.max(...pts);
  if (min === max) { min -= 1; max += 1; }
  const x = (i) => pad + (i * (W - 2 * pad)) / (pts.length - 1);
  const y = (v) => H - pad - ((v - min) / (max - min)) * (H - 2 * pad);
  const d = pts.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
  const c = color || "#0e4d7a";
  let zero = "";
  if (min < 0 && max > 0) {
    const zy = y(0).toFixed(1);
    zero = `<line x1="${pad}" y1="${zy}" x2="${W - pad}" y2="${zy}" stroke="#cbd5e0" stroke-width="0.6" stroke-dasharray="2 2"/>`;
  }
  const lx = x(pts.length - 1).toFixed(1), ly = y(pts[pts.length - 1]).toFixed(1);
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + zero
    + `<path d="${d}" fill="none" stroke="${c}" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`
    + `<circle cx="${lx}" cy="${ly}" r="1.7" fill="${c}" vector-effect="non-scaling-stroke"/>`
    + `</svg>`;
}
// Compact number for macro tiles: 219.88 -> "219.9", 914.69 -> "914.7".
function fmtBig(v) {
  if (v == null || isNaN(v)) return "–";
  return Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 });
}

/* Wrap a render call so one panel can never break the others. */
function safeRender(id, fn, data) {
  const el = document.getElementById(id);
  if (!el) return;
  try {
    fn(el, data);
  } catch (err) {
    console.error("render error in #" + id, err);
    el.innerHTML = `<div class="panel-head"><h2>${esc(id)}</h2></div>${nodata("Could not render this panel.")}`;
  }
}

/* ======================================================================
 * Panel 1 — Traffic by market (hero)
 * ==================================================================== */
function renderTraffic(el, d) {
  if (!d || !d.headline || !d.by_country) {
    el.innerHTML = `
      <div class="panel-head"><h2>Traffic by Market</h2></div>
      ${nodata("Traffic data not available yet. The daily refresh will populate this from CAAP's monthly release.")}`;
    return;
  }

  const h = d.headline, s = d.secondary, period = (d.period && d.period.label) || "";
  const maxC = Math.max(...d.by_country.map((c) => c.current || 0)) || 1;

  const countryRows = d.by_country.map((c) => {
    const w = Math.max(2, Math.round((c.current / maxC) * 100));
    const flag = FLAGS[c.name] || "";
    return `
      <div class="country-row">
        <div class="cname"><span class="flag">${flag}</span>${esc(c.name)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>
        <div class="cval"><span class="v">${fmtM(c.current)}</span>${pill(c.yoy_pct)}</div>
      </div>`;
  }).join("");

  const typeTiles = (d.by_type || []).map((t) => `
    <div class="tile">
      <div class="t-label">${esc(t.name)}</div>
      <div class="t-val">${fmtM(t.current)}</div>
      ${pill(t.yoy_pct)}
    </div>`).join("");
  const totalTile = d.total ? `
    <div class="tile total">
      <div class="t-label">Total</div>
      <div class="t-val">${fmtM(d.total.current)}</div>
      ${pill(d.total.yoy_pct)}
    </div>` : "";

  const ytd = h.ytd_yoy_pct != null
    ? `<div class="ytd">YTD international ${fmtPct(h.ytd_yoy_pct)} · total pax ${fmtPct(d.total && d.total.ytd_yoy_pct)} (${fmtM(d.total && d.total.ytd_current)} YTD)</div>`
    : "";

  el.innerHTML = `
    <div class="panel-head">
      <h2>Traffic by Market <span class="period">· ${esc(period)}</span></h2>
      ${stamp(d.updated_at, 45)}
    </div>
    <p class="subhead">International passengers is the headline; domestic is the secondary line. Figures in millions of passengers, year-over-year.</p>
    <div class="hero-top">
      <div class="headline">
        <div class="metric-label">${esc(h.metric || "International Passengers")}</div>
        <div class="big">${(h.current / 1000).toFixed(2)}<span class="unit">M</span></div>
        <div class="yoy-line">${pill(h.yoy_pct)} <span class="lbl">vs ${esc((d.period && d.period.prior_col) || "prior year")}</span></div>
        <div class="secondary">
          <span class="lbl">${esc((s && s.metric) || "Domestic")}:</span>
          <strong>${fmtM(s && s.current)}</strong> ${pill(s && s.yoy_pct)}
        </div>
        ${ytd}
      </div>
      <div class="country-block">
        <div class="country-list">${countryRows}</div>
      </div>
    </div>
    <div class="type-tiles">${typeTiles}${totalTile}</div>
    <div style="margin-top:12px">${srcLink(d.source_url, d.source || "SEC EDGAR 6-K")}</div>`;
}

/* ======================================================================
 * Panel 4 — Fuel / capacity risk ribbon
 * ==================================================================== */
function oilTile(o) {
  const price = o.price == null ? "–"
    : "$" + Number(o.price).toFixed(2) + (o.unit && o.unit.includes("gal") ? "/gal" : "");
  return `<span class="oil"><b>${esc(o.name)}</b> ${price} ${fuelPill(o.change_pct)}</span>`;
}
function renderRibbon(el, d) {
  if (!d || !d.oil || !d.oil.length) {
    el.innerHTML = `<div class="wrap ribbon-inner">
      <span class="tag">Fuel / capacity risk</span>
      <span class="muted">Brent · WTI · jet fuel and airline capacity-cut headlines — awaiting first refresh.</span></div>`;
    return;
  }
  const risk = d.risk || {};
  const level = (risk.level || "watch").toLowerCase();
  const tiles = d.oil.map(oilTile).join("");
  const heads = (d.headlines || []);
  const news = heads.length ? `
    <details class="cap-news">
      <summary>${risk.headline_count || heads.length} capacity-cut headlines ▾</summary>
      <ul>${heads.map((h) => `
        <li><a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(cleanTitle(h.title))}</a>
        <span class="src">${esc(h.source || "")}${h.published_iso ? " · " + esc(relTime(h.published_iso)) : ""}</span></li>`).join("")}
      </ul>
    </details>` : `<span class="muted">no capacity headlines</span>`;

  el.innerHTML = `<div class="wrap ribbon-inner">
    <span class="tag">Fuel / capacity risk</span>
    <span class="risk-badge ${level}" title="${esc(risk.rule || "")}">${esc(level)}</span>
    <span class="oil-tiles">${tiles}</span>
    ${news}
    <span class="stamp ribbon-stamp">${stamp(d.updated_at, 4).replace(/^<span[^>]*>|<\/span>$/g, "")}</span>
  </div>`;
}

/* ======================================================================
 * Panel 3 — Concession tracker (manual status live; news feed scaffold)
 * ==================================================================== */
function stepper(stages, current) {
  if (!Array.isArray(stages) || !stages.length) return "";
  const ci = stages.findIndex((s) => s.toLowerCase() === String(current).toLowerCase());
  return `<div class="stepper">` + stages.map((s, i) => {
    const cls = i < ci ? "done" : i === ci ? "current" : "";
    return `<span class="step ${cls}">${esc(s)}</span>`;
  }).join("") + `</div>`;
}
function subCardConcession(title, c, headlines) {
  if (!c) return "";
  return `
    <div class="sub-card">
      <h3>${esc(title)}</h3>
      ${stepper(c.stages, c.stage)}
      <p>${esc(c.note || "")}${c.expected ? ` <strong>· expected: ${esc(c.expected)}</strong>` : ""}</p>
      ${headlinesList(headlines, "No recent headlines.")}
    </div>`;
}
function renderConcession(el, d) {
  const status = d && d.status;
  const news = (d && d.news) || {};
  const c = status && status.concession;
  if (!c) {
    el.innerHTML = `<div class="panel-head"><h2>Concession Tracker</h2></div>${nodata("Status not set. Edit data/status.json to populate.")}`;
    return;
  }
  const topics = news.topics || {};
  const feedStamp = news.updated_at ? `<span class="stamp">headlines ${relTime(news.updated_at)}</span>` : "";
  el.innerHTML = `
    <div class="panel-head">
      <h2>Concession Tracker</h2>
      ${stamp(status.updated_at, 120)}
    </div>
    <p class="subhead">Stage is hand-set in <code>data/status.json</code>; headlines auto-update from Google News (local-language).</p>
    ${subCardConcession("Brazil · Brasília / JK re-concession", c.brasilia, topics.brasilia)}
    ${subCardConcession("Argentina · AA2000 (runs to 2038 — rebalancing, not renewal)", c.aa2000, topics.aa2000)}
    <div class="feed-foot">${srcLink(news.source_url, "Google News")} ${feedStamp}</div>`;
}

/* ======================================================================
 * Panel 5 — Armenia political watch (manual tension live; news feed scaffold)
 * ==================================================================== */
function tensionIndicator(level, levels) {
  const order = Array.isArray(levels) && levels.length ? levels : ["calm", "watch", "elevated", "high", "critical"];
  const idx = order.findIndex((l) => l.toLowerCase() === String(level).toLowerCase());
  const hot = idx >= 3;
  const dots = order.map((_, i) => `<span class="dot ${i <= idx ? "on" : ""}${i <= idx && hot ? " hot" : ""}"></span>`).join("");
  return `<div class="tension"><div class="dots">${dots}</div><span class="label">${esc(level || "unknown")}</span></div>`;
}
function renderArmenia(el, d) {
  const status = d && d.status;
  const news = (d && d.news) || {};
  const a = status && status.armenia;
  if (!a) {
    el.innerHTML = `<div class="panel-head"><h2>Armenia Political Watch</h2></div>${nodata("Tension level not set. Edit data/status.json to populate.")}`;
    return;
  }
  const feedStamp = news.updated_at ? `<span class="stamp">headlines ${relTime(news.updated_at)}</span>` : "";
  el.innerHTML = `
    <div class="panel-head">
      <h2>Armenia Political Watch</h2>
      ${stamp(status.updated_at, 120)}
    </div>
    <p class="subhead">Escalation risk around the Armenia–Azerbaijan peace process. Tension level is hand-set in <code>data/status.json</code>.</p>
    ${tensionIndicator(a.tension_level, a.levels)}
    <div class="sub-card"><p>${esc(a.note || "")}${a.as_of ? ` <strong>· as of ${esc(a.as_of)}</strong>` : ""}</p></div>
    ${headlinesList(news.headlines, "No recent headlines.")}
    <div class="feed-foot">${srcLink(news.source_url, "Google News")} ${feedStamp}</div>`;
}

/* ======================================================================
 * Panel 2 — GDP growth (+ Argentina inflation / FX)
 * ==================================================================== */
function gdpTile(c) {
  const cls = pctClass(c.latest);
  const col = cls === "neg" ? "#c0392b" : cls === "pos" ? "#1a7f4b" : "#5b6b7c";
  return `
    <div class="gdp-tile">
      <div class="g-country">${FLAGS[c.name] || ""} ${esc(c.name)}</div>
      <div class="g-val ${cls}">${c.latest != null ? fmtPct(c.latest) : "–"}</div>
      <div class="g-year">real GDP growth · ${esc(c.year || "—")}</div>
      ${sparkline(c.series, col)}
    </div>`;
}
function macroTile(m, color) {
  if (!m) return "";
  const prev = m.series && m.series.length > 1 ? m.series[m.series.length - 2] : null;
  const trend = prev ? ` <span class="m-prev">was ${fmtBig(prev.value)} in ${esc(prev.year)}</span>` : "";
  return `
    <div class="macro-tile">
      <div class="m-label">${esc(m.label || "")}</div>
      <div class="m-val">${fmtBig(m.latest)}<span class="m-year"> · ${esc(m.year || "—")}</span></div>
      ${sparkline(m.series, color)}
      <div class="m-foot">${trend}</div>
    </div>`;
}
function renderGDP(el, d) {
  if (!d || !d.countries) {
    const markets = ["Argentina", "Brazil", "Italy", "Armenia"];
    const tiles = markets.map((m) => `
      <div class="gdp-tile">
        <div class="g-country">${FLAGS[m] || ""} ${esc(m)}</div>
        <div class="g-val">–</div>
        <div class="g-year">real GDP growth · awaiting data</div>
      </div>`).join("");
    el.innerHTML = `
      <div class="panel-head"><h2>GDP Growth</h2></div>
      <p class="subhead">Real GDP growth (World Bank) for CAAP's key markets, plus Argentina inflation &amp; FX. Awaiting first refresh.</p>
      <div class="gdp-grid">${tiles}</div>`;
    return;
  }

  const tiles = d.countries.map(gdpTile).join("");
  const arg = d.argentina || {};
  const macro = (arg.inflation || arg.fx) ? `
    <div class="gdp-macro">
      <div class="macro-head">🇦🇷 Argentina macro watch <span class="muted">— the dominant swing factor for CAAP's largest market</span></div>
      <div class="macro-grid">
        ${macroTile(arg.inflation, "#b7791f")}
        ${macroTile(arg.fx, "#c0392b")}
      </div>
      <p class="macro-note">World Bank annual figures — inflation is the CPI year-average; FX is the official ARS/USD period average (lags the spot rate). Directional, not real-time.</p>
    </div>` : "";

  el.innerHTML = `
    <div class="panel-head">
      <h2>GDP Growth <span class="period">· latest ${esc(d.countries[0].year || "")}</span></h2>
      ${stamp(d.updated_at, 400)}
    </div>
    <p class="subhead">Real GDP growth across CAAP's GDP-relevant markets (World Bank). Sparklines show the last decade.</p>
    <div class="gdp-grid">${tiles}</div>
    ${macro}
    <div style="margin-top:14px">${srcLink(d.source_url, d.source || "World Bank")}</div>`;
}

/* ---------- boot ---------- */
async function boot() {
  const [traffic, gdp, concession, fuel, armenia, status] = await Promise.all([
    loadJSON(DATA_FILES.traffic),
    loadJSON(DATA_FILES.gdp),
    loadJSON(DATA_FILES.concession),
    loadJSON(DATA_FILES.fuel),
    loadJSON(DATA_FILES.armenia),
    loadJSON(DATA_FILES.status),
  ]);

  safeRender("traffic", renderTraffic, traffic);
  safeRender("ribbon", renderRibbon, fuel);
  safeRender("concession", renderConcession, { status, news: concession });
  safeRender("armenia", renderArmenia, { status, news: armenia });
  safeRender("gdp", renderGDP, gdp);
}

document.addEventListener("DOMContentLoaded", boot);
