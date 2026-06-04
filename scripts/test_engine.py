import datetime as dt
from fetch_flights import (
    analyze, carrier_of, dedup, append_point, carrier_split_of, WINDOW_HOURS,
)

# This test pins `now` to midnight UTC so each WINDOW_HOURS-long window the engine
# pulls is a single midnight-aligned day-chunk whose `begin` equals the window
# start — which lets the mock key its canned lists by (airport, window-begin).
assert WINDOW_HOURS == 24, "test assumes the default 24h single-chunk window"

# --- unit checks on the helpers ---
assert carrier_of("GLO1234") == "GOL"
assert carrier_of("TAM3300") == "LATAM"
assert carrier_of("LAN401")  == "LATAM"
assert carrier_of("AZU4567") == "Azul"
assert carrier_of("ARG1300") == "Aerolineas Argentinas"
assert carrier_of("AFL512")  == "Other"        # Aeroflot not in the Brazil split
assert carrier_of(None)      == "Other"
assert len(dedup([{"icao24": "a", "firstSeen": 1}, {"icao24": "a", "firstSeen": 1}])) == 1

# carrier_split_of: largest group first, current-only counts (no YoY)
cs = carrier_split_of([{"callsign": "GLO1"}] * 3 + [{"callsign": "TAM2"}] * 5)
assert cs[0] == {"carrier": "LATAM", "current": 5}, cs
assert cs[1] == {"carrier": "GOL", "current": 3}, cs

# append_point: dedup by date (same-day re-run overwrites), sorted, capped
h = append_point([], {"date": "2026-06-01", "movements": 10})
h = append_point(h, {"date": "2026-06-02", "movements": 20})
h = append_point(h, {"date": "2026-06-02", "movements": 25})   # same day → overwrite
assert [p["movements"] for p in h] == [10, 25], h
assert len(append_point([{"date": f"d{i}", "movements": i} for i in range(200)],
                        {"date": "d999", "movements": 1}, cap=120)) == 120
print("helper checks passed")


# --- mock OpenSky: canned arrival lists keyed by (airport, window-begin) ---
def make_flight(icao24, callsign, origin):
    return {"icao24": icao24, "firstSeen": hash((icao24, origin)) & 0xffff,
            "callsign": callsign, "estDepartureAirport": origin, "estArrivalAirport": "X"}

NOW1 = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
NOW2 = dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc)   # next day
B1 = int((NOW1 - dt.timedelta(hours=WINDOW_HOURS)).timestamp())   # day-1 window start
B2 = int((NOW2 - dt.timedelta(hours=WINDOW_HOURS)).timestamp())   # day-2 window start


def sbbr(day):
    # Brasília carrier mix; both days total 95 movements but the split shifts,
    # so the per-day carrier_split is a real, changing reading.
    mix = (["GLO1"] * 40 + ["TAM2"] * 35 + ["AZU3"] * 15 + ["ARG9"] * 5) if day == 1 \
        else (["GLO1"] * 44 + ["TAM2"] * 33 + ["AZU3"] * 12 + ["ARG9"] * 6)
    return [make_flight(f"{day}c{i}", cs, "SBSP") for i, cs in enumerate(mix)]


def udyz(day):
    ru = 12 if day == 1 else 9        # Russia-origin arrivals (tailwind eroding)
    other = 8 if day == 1 else 10
    return ([make_flight(f"{day}r{i}", "RU", "UUEE") for i in range(ru)] +
            [make_flight(f"{day}a{i}", "AIR", "LIRQ") for i in range(other)])


CANNED = {
    ("SBBR", B1): sbbr(1), ("SBBR", B2): sbbr(2),
    ("UDYZ", B1): udyz(1), ("UDYZ", B2): udyz(2),
    ("LIRQ", B1): [make_flight(f"f{i}", "X", "Y") for i in range(50)],
    ("LIRQ", B2): [make_flight(f"g{i}", "X", "Y") for i in range(53)],
}
# Every other airport returns nothing → a completed-but-empty pull → establishing
# baseline (movements 0), never a false reading.


def mock_fetch(path, params, token):
    if not path.endswith("arrival"):
        return []                          # departures empty; movements come from arrivals
    begin = params["begin"]
    if begin in (B1, B2):                  # only the window's first (here, only) chunk carries data
        return CANNED.get((params["airport"], begin), [])
    return []


# --- Day 1 (cold start) ---
day1 = analyze(fetch=mock_fetch, now=NOW1, prev=None)
a1 = {a["icao"]: a for a in day1["airports"]}

bbr = a1["SBBR"]
assert bbr["latest"]["movements"] == 95, bbr["latest"]
assert len(bbr["history"]) == 1, bbr["history"]
split = {c["carrier"]: c["current"] for c in bbr["latest"]["carrier_split"]}
assert split["GOL"] == 40 and split["LATAM"] == 35 and split["Azul"] == 15, split
print("Day1 Brasília:", bbr["latest"]["movements"], split)

udz = a1["UDYZ"]
assert udz["latest"]["movements"] == 20, udz["latest"]
assert udz["latest"]["russia_inflow"]["current"] == 12, udz["latest"]["russia_inflow"]
print("Day1 Yerevan:", udz["latest"]["movements"], "| russia:", udz["latest"]["russia_inflow"]["current"])

assert a1["LIRQ"]["latest"]["movements"] == 50, a1["LIRQ"]["latest"]

# Establishing-baseline airport: a completed-but-empty pull is a real measured 0,
# NOT a false reading — the panel renders it as "establishing baseline", no count.
assert a1["SACO"]["latest"]["movements"] == 0, a1["SACO"]["latest"]
assert a1["SACO"]["last_complete"] is True, a1["SACO"]
assert day1["any_reading"] is True
assert day1["developing"] is True
print("Day1 readings:", {k: (v["latest"]["movements"] if v["latest"] else None) for k, v in a1.items()})

# --- Day 2 (accumulates onto Day 1) ---
day2 = analyze(fetch=mock_fetch, now=NOW2, prev=day1)
a2 = {a["icao"]: a for a in day2["airports"]}

assert len(a2["SBBR"]["history"]) == 2, a2["SBBR"]["history"]
assert [p["movements"] for p in a2["LIRQ"]["history"]] == [50, 53], a2["LIRQ"]["history"]
assert a2["LIRQ"]["latest"]["movements"] == 53
assert a2["UDYZ"]["latest"]["russia_inflow"]["current"] == 9, a2["UDYZ"]["latest"]["russia_inflow"]
# Day-2 Brasília carrier split is the new day's reading, not last year's.
split2 = {c["carrier"]: c["current"] for c in a2["SBBR"]["latest"]["carrier_split"]}
assert split2["GOL"] == 44 and split2["Azul"] == 12, split2
print("Day2 Florence trend:", [p["movements"] for p in a2["LIRQ"]["history"]])

# --- Re-run Day 2 (same date) → dedup keeps the trend length stable ---
day2b = analyze(fetch=mock_fetch, now=NOW2, prev=day2)
a2b = {a["icao"]: a for a in day2b["airports"]}
assert len(a2b["LIRQ"]["history"]) == 2, a2b["LIRQ"]["history"]
print("Same-day re-run keeps Florence history at", len(a2b["LIRQ"]["history"]), "points")

print("auth field:", day1["auth"], "| developing:", day1["developing"],
      "| any_reading:", day1["any_reading"])
print("\nALL ASSERTIONS PASSED")
