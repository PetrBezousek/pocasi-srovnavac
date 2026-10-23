"""Nastavení srovnávače. Pro jiné místo stačí změnit tuto sekci."""

# Místo, pro které se sbírají předpovědi (souřadnice stanice ČHMÚ,
# aby se předpověď porovnávala přesně s místem měření).
LOCATION_NAME = "Ústí nad Orlicí"
LAT = 49.9803
LON = 16.4222

# Stanice ČHMÚ, ze které se berou skutečně naměřené hodnoty.
# Seznam stanic: https://opendata.chmi.cz/meteorology/climate/now/metadata/
CHMI_STATION_WSI = "0-20000-0-11679"
CHMI_STATION_NAME = "Ústí nad Orlicí (profesionální stanice ČHMÚ, 402 m n. m.)"

# Jak daleko dopředu se předpovědi ukládají (hodiny).
HORIZON_H = 72

# Modely z Open-Meteo, které se testují každý zvlášť.
# Každý se v žebříčku objeví jako samostatný soutěžící.
OPEN_METEO_MODELS = {
    "best_match": "Open-Meteo (automatický výběr)",
    "icon_d2": "Open-Meteo: DWD ICON-D2 (2 km)",
    "icon_eu": "Open-Meteo: DWD ICON-EU (7 km)",
    "ecmwf_ifs025": "Open-Meteo: ECMWF IFS",
    "ecmwf_aifs025_single": "Open-Meteo: ECMWF AIFS (AI model)",
    "gfs_seamless": "Open-Meteo: NOAA GFS",
    "meteofrance_seamless": "Open-Meteo: Météo-France ARPEGE/AROME",
    "ukmo_seamless": "Open-Meteo: UK Met Office",
    "knmi_seamless": "Open-Meteo: KNMI HARMONIE",
    "dmi_seamless": "Open-Meteo: DMI HARMONIE",
    "gem_seamless": "Open-Meteo: Kanada GEM",
}

# Popisky ostatních poskytovatelů.
PROVIDER_LABELS = {
    "metno": "MET Norway (Yr)",
    "brightsky": "Bright Sky (DWD MOSMIX)",
    "openweathermap": "OpenWeatherMap",
    "weatherapi": "WeatherAPI.com",
    "visualcrossing": "Visual Crossing",
}
