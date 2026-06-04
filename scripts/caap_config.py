"""
CAAPWATCH flight-engine configuration.

Maps CAAP's key concession airports to ICAO codes, tags each with the thesis
role it informs and how confident we are in ADS-B coverage there, and defines
the airline-callsign prefixes and Russian-airport set used to decompose
movements. Everything downstream keys off this file, so this is the one place
to edit when adding an airport or a carrier.
"""

# CAAP key airports. coverage: "high" = dense ADS-B (Europe / major hubs),
# "medium" = expect usable but verify on first run, "verify" = unknown, check
# the first Actions run before relying on it.
AIRPORTS = [
    # Argentina (AA2000 — ~85% of attributable EBITDA; the bridge engine)
    {"icao": "SAEZ", "name": "Buenos Aires Ezeiza",   "country": "Argentina", "coverage": "medium", "intl_gateway": True},
    {"icao": "SABE", "name": "Buenos Aires Aeroparque","country": "Argentina", "coverage": "medium", "intl_gateway": False},
    {"icao": "SACO", "name": "Córdoba",                "country": "Argentina", "coverage": "medium", "intl_gateway": False},
    # Brazil (Brasília — the catalyst asset; exposed to GOL/LATAM)
    {"icao": "SBBR", "name": "Brasília",               "country": "Brazil",    "coverage": "medium", "intl_gateway": True, "brazil_carrier_split": True},
    # Italy (Toscana — the only place the team thinks fuel actually bites)
    {"icao": "LIRQ", "name": "Florence",               "country": "Italy",     "coverage": "high",   "intl_gateway": True},
    {"icao": "LIRP", "name": "Pisa",                   "country": "Italy",     "coverage": "high",   "intl_gateway": True},
    # Armenia (the Russian-rerouting tailwind)
    {"icao": "UDYZ", "name": "Yerevan Zvartnots",      "country": "Armenia",   "coverage": "verify", "intl_gateway": True, "track_russia_inflow": True},
    # Ecuador / Uruguay (smaller, rounding out the network)
    {"icao": "SEGU", "name": "Guayaquil",              "country": "Ecuador",   "coverage": "medium", "intl_gateway": True},
    {"icao": "SUMU", "name": "Montevideo Carrasco",    "country": "Uruguay",   "coverage": "medium", "intl_gateway": True},
]

# ICAO airline-callsign prefixes -> carrier group. Used to decompose Brazil
# movements (the GOL/LATAM/Azul volume-risk signal in ask #4).
CARRIER_PREFIXES = {
    "GLO": "GOL",
    "TAM": "LATAM", "LAN": "LATAM", "LATAM": "LATAM",
    "AZU": "Azul",
    "ARG": "Aerolineas Argentinas",
    "FBZ": "Flybondi",
    "JES": "JetSMART", "JAR": "JetSMART",
}

# Major Russian-airport ICAOs. An arrival at Yerevan whose origin is in this set
# is counted as Russia-inflow (the Armenia tailwind in ask #5). Russian ICAOs
# are spread across several U-prefixes, so we enumerate the big ones explicitly
# rather than pattern-match (U also covers Armenia, Georgia, Ukraine, etc.).
RUSSIA_AIRPORTS = {
    "UUEE",  # Moscow Sheremetyevo
    "UUDD",  # Moscow Domodedovo
    "UUWW",  # Moscow Vnukovo
    "ULLI",  # St Petersburg Pulkovo
    "URSS",  # Sochi
    "UWWW",  # Samara
    "USSS",  # Yekaterinburg Koltsovo
    "UNNT",  # Novosibirsk
    "URRR",  # Rostov
    "UWUU",  # Ufa
    "URWA",  # Astrakhan
    "URMG",  # Grozny
    "URML",  # Makhachkala
    "UWKD",  # Kazan
    "URKK",  # Krasnodar
    "UHWW",  # Vladivostok
    "UWGG",  # Nizhny Novgorod
}

# Window settings. OpenSky's arrival/departure endpoint caps each request at a
# 7-day interval, so we use a trailing 7 full days and compare to the same 7
# days one year earlier for a clean YoY read.
WINDOW_DAYS = 7
