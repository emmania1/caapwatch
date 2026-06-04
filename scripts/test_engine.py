import datetime as dt
from fetch_flights import analyze, carrier_of, dedup

# --- unit checks on the helpers ---
assert carrier_of("GLO1234") == "GOL"
assert carrier_of("TAM3300") == "LATAM"
assert carrier_of("LAN401")  == "LATAM"
assert carrier_of("AZU4567") == "Azul"
assert carrier_of("ARG1300") == "Aerolineas Argentinas"
assert carrier_of("AFL512")  == "Other"        # Aeroflot not in Brazil split
assert carrier_of(None)      == "Other"
assert len(dedup([{"icao24":"a","firstSeen":1},{"icao24":"a","firstSeen":1}])) == 1
print("helper checks passed")

# --- mock OpenSky: return canned flight lists keyed by (path, airport, year) ---
def make_flight(icao24, callsign, origin):
    return {"icao24": icao24, "firstSeen": hash((icao24,origin)) & 0xffff,
            "callsign": callsign, "estDepartureAirport": origin, "estArrivalAirport": "X"}

# Define current-year vs prior-year movement counts per airport to test YoY.
SCENARIO = {
    # airport: (current arrivals list, prior-year arrivals list)
    "SBBR": (
        [make_flight(f"c{i}", cs, "SBSP") for i,cs in enumerate(
            ["GLO1"]*40 + ["TAM2"]*35 + ["AZU3"]*15 + ["ARG9"]*5)],   # 95 cur
        [make_flight(f"p{i}", cs, "SBSP") for i,cs in enumerate(
            ["GLO1"]*30 + ["TAM2"]*30 + ["AZU3"]*25 + ["ARG9"]*5)],   # 90 py
    ),
    "UDYZ": (
        [make_flight(f"c{i}","RU","UUEE") for i in range(12)] +       # 12 from Russia
        [make_flight(f"c2{i}","AIR","LIRQ") for i in range(8)],        # 8 non-Russia -> 20 cur
        [make_flight(f"p{i}","RU","UUDD") for i in range(18)] +        # 18 from Russia (higher last yr)
        [make_flight(f"p2{i}","AIR","LIRQ") for i in range(7)],        # 25 py
    ),
    "LIRQ": ([make_flight(f"c{i}","X","Y") for i in range(50)],
             [make_flight(f"p{i}","X","Y") for i in range(44)]),
}
DEFAULT = ([make_flight("c","X","Y")]*10, [make_flight("p","X","Y")]*10)

# The engine now pulls one midnight-aligned UTC-day chunk at a time (OpenSky's
# 2-partition limit) and sums across chunks, so mock_fetch is called once per
# day-chunk per window — not once per window. With `now` pinned to midnight UTC
# each 7-day window is exactly 7 full-day chunks; we hand the whole canned list
# to just the FIRST chunk of each window (begin == the window's start) and
# return [] for every other chunk, so the summed totals are identical to before.
NOW = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
CUR_BEGIN = int((NOW - dt.timedelta(days=7)).timestamp())     # current-window start
PY_BEGIN = int((NOW - dt.timedelta(days=372)).timestamp())    # prior-year start (365 + 7)

def mock_fetch(path, params, token):
    # only the arrival endpoint carries origin info we test; give departures empty
    if not path.endswith("arrival"):
        return []
    cur, py = SCENARIO.get(params["airport"], DEFAULT)
    begin = params["begin"]
    if begin == CUR_BEGIN:
        return cur
    if begin == PY_BEGIN:
        return py
    return []

payload = analyze(fetch=mock_fetch, now=NOW)

byicao = {a["icao"]: a for a in payload["airports"]}

# Brasília YoY: 95 vs 90 -> +5.6%
bbr = byicao["SBBR"]
assert bbr["movements_current"] == 95 and bbr["movements_prior_year"] == 90, bbr
assert bbr["yoy_pct"] == 5.6, bbr["yoy_pct"]
cs = {c["carrier"]: c for c in bbr["carrier_split"]}
assert cs["GOL"]["current"] == 40 and cs["GOL"]["prior_year"] == 30, cs["GOL"]
assert cs["LATAM"]["current"] == 35, cs["LATAM"]
assert cs["Azul"]["yoy_pct"] == -40.0, cs["Azul"]   # 15 vs 25 -> -40%
print("Brasília carrier split:", {k:(v['current'],v['prior_year'],v['yoy_pct']) for k,v in cs.items()})

# Yerevan Russia inflow: 12 vs 18 -> -33.3% (tailwind eroding signal)
udyz = byicao["UDYZ"]
assert udyz["russia_inflow"]["current"] == 12 and udyz["russia_inflow"]["prior_year"] == 18, udyz["russia_inflow"]
assert udyz["russia_inflow"]["yoy_pct"] == -33.3, udyz["russia_inflow"]
print("Yerevan russia_inflow:", udyz["russia_inflow"], "| total movements:", udyz["movements_current"])

# Florence simple YoY 50 vs 44 -> +13.6
assert byicao["LIRQ"]["yoy_pct"] == 13.6
print("Florence yoy:", byicao["LIRQ"]["yoy_pct"])

# Reliability: airports with a real base in both windows are trustworthy; a
# barely-covered airport (DEFAULT → 1 movement after dedup, below TRUST_MIN) is
# flagged so the panel never presents a fraction-of-reality count as signal.
assert bbr["reliable"] is True, bbr
assert byicao["SACO"]["reliable"] is False, byicao["SACO"]   # default low-coverage airport
assert payload["reliable"] is True   # at least one airport is trustworthy
print("reliable flags:", {a["icao"]: a["reliable"] for a in payload["airports"]})
print("auth field:", payload["auth"], "| payload reliable:", payload["reliable"])
print("\nALL ASSERTIONS PASSED")
