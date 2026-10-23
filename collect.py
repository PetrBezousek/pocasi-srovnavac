"""Sběr předpovědí ze všech poskytovatelů a naměřených hodnot ČHMÚ.

Spouští se pravidelně (GitHub Actions každé 3 hodiny). Používá jen
standardní knihovnu Pythonu, nic se nemusí instalovat.

Výstupy:
  data/forecasts/YYYY-MM-DD.csv   předpovědi podle dne vydání
  data/observations.csv           hodinová měření ze stanice ČHMÚ
  data/runs.csv                   log každého volání API (úspěch, odezva)
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FORECAST_DIR = DATA / "forecasts"
OBS_FILE = DATA / "observations.csv"
RUNS_FILE = DATA / "runs.csv"

FORECAST_FIELDS = ["issued_utc", "provider", "valid_utc", "lead_h",
                   "temp", "rh", "wind", "precip", "precip_h"]
OBS_FIELDS = ["valid_utc", "temp", "rh", "wind", "precip"]
RUN_FIELDS = ["run_utc", "provider", "ok", "rows", "latency_ms", "error"]

UTC = timezone.utc
USER_AGENT = "pocasi-srovnavac/1.0 (+https://github.com/{})".format(
    os.environ.get("GITHUB_REPOSITORY", "unknown/pocasi-srovnavac"))


# ---------------------------------------------------------------- pomocné

def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value) -> datetime:
    """Přijme ISO řetězec (s 'Z' nebo offsetem) nebo unix čas."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def num(value):
    """Převede hodnotu na float, prázdné/neplatné vrátí None."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def rh_from_dewpoint(t, td):
    """Relativní vlhkost z teploty a rosného bodu (Magnusův vzorec)."""
    if t is None or td is None:
        return None
    a, b = 17.625, 243.04
    rh = 100 * math.exp(a * td / (b + td)) / math.exp(a * t / (b + t))
    return max(0.0, min(100.0, rh))


def http_json(url: str, headers: dict | None = None, retries: int = 2):
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                break  # opakování nepomůže
        except Exception as e:  # síť, timeout, špatný JSON
            last = e
        time.sleep(3 * (attempt + 1))
    raise last


def make_row(provider, issued, valid, temp=None, rh=None, wind=None,
             precip=None, precip_h=None):
    lead = round((valid - issued).total_seconds() / 3600)
    return {
        "provider": provider,
        "issued_utc": iso(issued),
        "valid_utc": iso(valid),
        "lead_h": lead,
        "temp": temp, "rh": rh, "wind": wind,
        "precip": precip, "precip_h": precip_h if precip is not None else None,
    }


def in_horizon(issued, valid):
    lead = (valid - issued).total_seconds() / 3600
    return 1 <= lead <= config.HORIZON_H


def merge_by_valid(entries):
    """Sloučí částečné záznamy (např. teplota a srážky z různých kroků)."""
    out = {}
    for valid, fields in entries:
        cur = out.setdefault(valid, {})
        for k, v in fields.items():
            if v is not None:
                cur[k] = v
    return out


# --------------------------------------------------------- poskytovatelé
#
# Každý parser dostane surový JSON a čas vydání a vrátí seznam řádků.
# Srážky se ukládají jako úhrn za `precip_h` hodin KONČÍCÍCH v čase valid_utc.

def parse_open_meteo(data, issued):
    hourly = data.get("hourly", {})
    times = [parse_time(t) for t in hourly.get("time", [])]
    models = list(config.OPEN_METEO_MODELS)
    single = len(models) == 1
    rows = []
    for model in models:
        # Při více modelech má každá proměnná příponu, např. temperature_2m_icon_d2.
        def series(var):
            return hourly.get(f"{var}_{model}") or (hourly.get(var) if single else None) or []
        t2m = series("temperature_2m")
        rh = series("relative_humidity_2m")
        ws = series("wind_speed_10m")
        pr = series("precipitation")
        for i, valid in enumerate(times):
            if not in_horizon(issued, valid):
                continue
            vals = [x[i] if i < len(x) else None for x in (t2m, rh, ws, pr)]
            if all(v is None for v in vals):
                continue  # model tuto hodinu/oblast nepokrývá
            p = num(vals[3])
            rows.append(make_row(f"openmeteo_{model}", issued, valid,
                                 num(vals[0]), num(vals[1]), num(vals[2]),
                                 p, 1 if p is not None else None))
    return rows


def fetch_open_meteo(issued):
    params = {
        "latitude": config.LAT, "longitude": config.LON,
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
        "models": ",".join(config.OPEN_METEO_MODELS),
        "wind_speed_unit": "ms", "timezone": "GMT",
        "forecast_days": math.ceil(config.HORIZON_H / 24) + 1,
    }
    base = "https://api.open-meteo.com/v1/forecast?"
    try:
        return parse_open_meteo(http_json(base + urllib.parse.urlencode(params)), issued)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
    # Některý název modelu API nezná: stáhnout modely po jednom a vadné přeskočit.
    rows, bad = [], []
    for model in config.OPEN_METEO_MODELS:
        try:
            data = http_json(base + urllib.parse.urlencode({**params, "models": model}), retries=0)
        except urllib.error.HTTPError:
            bad.append(model)
            continue
        # Při jednom modelu nemají proměnné příponu -> doplnit ji.
        hourly = data.get("hourly", {})
        data["hourly"] = {(k if k == "time" else f"{k}_{model}"): v for k, v in hourly.items()}
        rows += [r for r in parse_open_meteo(data, issued) if r["provider"] == f"openmeteo_{model}"]
    if bad:
        print("Open-Meteo: neznámé modely " + ", ".join(bad))
    return rows


def parse_metno(data, issued):
    entries = []
    for step in data.get("properties", {}).get("timeseries", []):
        t = parse_time(step["time"])
        d = step.get("data", {})
        inst = d.get("instant", {}).get("details", {})
        entries.append((t, {"temp": num(inst.get("air_temperature")),
                            "rh": num(inst.get("relative_humidity")),
                            "wind": num(inst.get("wind_speed"))}))
        # Srážky jsou uvedeny na následující interval -> přiřadit jeho konci.
        n1 = d.get("next_1_hours", {}).get("details", {})
        if "precipitation_amount" in n1:
            entries.append((t + timedelta(hours=1),
                            {"precip": num(n1["precipitation_amount"]), "precip_h": 1}))
        elif "next_6_hours" in d:
            n6 = d["next_6_hours"].get("details", {})
            if "precipitation_amount" in n6:
                entries.append((t + timedelta(hours=6),
                                {"precip": num(n6["precipitation_amount"]), "precip_h": 6}))
    rows = []
    for valid, f in sorted(merge_by_valid(entries).items()):
        if in_horizon(issued, valid):
            rows.append(make_row("metno", issued, valid, f.get("temp"), f.get("rh"),
                                 f.get("wind"), f.get("precip"), f.get("precip_h")))
    return rows


def fetch_metno(issued):
    url = ("https://api.met.no/weatherapi/locationforecast/2.0/compact?"
           f"lat={config.LAT:.4f}&lon={config.LON:.4f}")
    return parse_metno(http_json(url), issued)


def parse_brightsky(data, issued):
    rows = []
    forecast_sources = {s["id"] for s in data.get("sources", [])
                        if s.get("observation_type") == "forecast"}
    for w in data.get("weather", []):
        if forecast_sources and w.get("source_id") not in forecast_sources:
            continue
        valid = parse_time(w["timestamp"])
        if not in_horizon(issued, valid):
            continue
        t = num(w.get("temperature"))
        rh = num(w.get("relative_humidity"))
        if rh is None:
            rh = rh_from_dewpoint(t, num(w.get("dew_point")))
        ws = num(w.get("wind_speed"))
        p = num(w.get("precipitation"))  # úhrn za předchozích 60 minut
        rows.append(make_row("brightsky", issued, valid, t, rh,
                             ws / 3.6 if ws is not None else None,
                             p, 1 if p is not None else None))
    return rows


def fetch_brightsky(issued):
    start = issued.strftime("%Y-%m-%dT%H:%M")
    end = (issued + timedelta(hours=config.HORIZON_H)).strftime("%Y-%m-%dT%H:%M")
    url = ("https://api.brightsky.dev/weather?" + urllib.parse.urlencode({
        "lat": config.LAT, "lon": config.LON, "date": start,
        "last_date": end, "max_dist": 60000, "tz": "Etc/UTC"}))
    return parse_brightsky(http_json(url), issued)


def parse_openweathermap(data, issued):
    rows = []
    for item in data.get("list", []):
        valid = parse_time(item["dt"])
        if not in_horizon(issued, valid):
            continue
        main = item.get("main", {})
        precip = 0.0
        for k in ("rain", "snow"):
            precip += num(item.get(k, {}).get("3h")) or 0.0
        # Dokumentace: úhrn za "posledních 3 hodin" -> končí v čase dt.
        rows.append(make_row("openweathermap", issued, valid,
                             num(main.get("temp")), num(main.get("humidity")),
                             num(item.get("wind", {}).get("speed")), precip, 3))
    return rows


def fetch_openweathermap(issued, key):
    url = ("https://api.openweathermap.org/data/2.5/forecast?" + urllib.parse.urlencode({
        "lat": config.LAT, "lon": config.LON, "appid": key, "units": "metric"}))
    return parse_openweathermap(http_json(url), issued)


def parse_weatherapi(data, issued):
    rows = []
    for day in data.get("forecast", {}).get("forecastday", []):
        for h in day.get("hour", []):
            start = parse_time(h["time_epoch"])
            valid = start
            wind = num(h.get("wind_kph"))
            row_inst = (valid, {"temp": num(h.get("temp_c")), "rh": num(h.get("humidity")),
                                "wind": wind / 3.6 if wind is not None else None})
            # Úhrn za hodinu začínající v čase položky -> končí o hodinu později.
            row_p = (start + timedelta(hours=1),
                     {"precip": num(h.get("precip_mm")), "precip_h": 1})
            rows.extend([row_inst, row_p])
    out = []
    for valid, f in sorted(merge_by_valid(rows).items()):
        if in_horizon(issued, valid):
            out.append(make_row("weatherapi", issued, valid, f.get("temp"), f.get("rh"),
                                f.get("wind"), f.get("precip"), f.get("precip_h")))
    return out


def fetch_weatherapi(issued, key):
    url = ("https://api.weatherapi.com/v1/forecast.json?" + urllib.parse.urlencode({
        "key": key, "q": f"{config.LAT},{config.LON}",
        "days": 3 if config.HORIZON_H <= 72 else 4, "aqi": "no", "alerts": "no"}))
    return parse_weatherapi(http_json(url), issued)


def parse_visualcrossing(data, issued):
    rows = []
    for day in data.get("days", []):
        for h in day.get("hours", []):
            start = parse_time(h["datetimeEpoch"])
            wind = num(h.get("windspeed"))
            rows.append((start, {"temp": num(h.get("temp")), "rh": num(h.get("humidity")),
                                 "wind": wind / 3.6 if wind is not None else None}))
            rows.append((start + timedelta(hours=1),
                         {"precip": num(h.get("precip")), "precip_h": 1}))
    out = []
    for valid, f in sorted(merge_by_valid(rows).items()):
        if in_horizon(issued, valid):
            out.append(make_row("visualcrossing", issued, valid, f.get("temp"), f.get("rh"),
                                f.get("wind"), f.get("precip"), f.get("precip_h")))
    return out


def fetch_visualcrossing(issued, key):
    # next3days = 4 dny × 24 h = 96 "záznamů" na volání; 8 běhů denně = 768,
    # tedy pod limitem 1000 záznamů denně ve free tarifu.
    url = ("https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
           f"timeline/{config.LAT},{config.LON}/next3days?" + urllib.parse.urlencode({
               "unitGroup": "metric", "include": "hours", "key": key,
               "contentType": "json",
               "elements": "datetimeEpoch,temp,humidity,windspeed,precip"}))
    return parse_visualcrossing(http_json(url), issued)


def providers():
    """Seznam (jméno, funkce). Placená/klíčová API jen když je nastaven klíč."""
    lst = [("openmeteo", fetch_open_meteo),
           ("metno", fetch_metno),
           ("brightsky", fetch_brightsky)]
    keyed = [("openweathermap", "OPENWEATHERMAP_API_KEY", fetch_openweathermap),
             ("weatherapi", "WEATHERAPI_API_KEY", fetch_weatherapi),
             ("visualcrossing", "VISUALCROSSING_API_KEY", fetch_visualcrossing)]
    for name, env, fn in keyed:
        key = os.environ.get(env, "").strip()
        if key:
            lst.append((name, lambda issued, fn=fn, key=key: fn(issued, key)))
    return lst


# ------------------------------------------------------------ měření ČHMÚ

def parse_chmi_10m(data):
    """Z 10minutových dat ČHMÚ udělá hodinové hodnoty.

    Teplota, vlhkost a vítr = hodnota v celou hodinu.
    Srážky = součet šesti 10min úhrnů končících v dané hodině
    (jen pokud je všech šest k dispozici).
    """
    payload = data["data"]["data"]
    header = payload["header"].split(",")
    i_el, i_dt, i_val = header.index("ELEMENT"), header.index("DT"), header.index("VAL")
    series = {"T": {}, "H": {}, "F": {}, "SRA10M": {}}
    for row in payload["values"]:
        el = row[i_el]
        if el in series:
            series[el][parse_time(row[i_dt])] = num(row[i_val])
    hours = sorted({t for t in series["T"] if t.minute == 0}
                   | {t for t in series["SRA10M"] if t.minute == 0})
    out = {}
    for h in hours:
        rec = {"temp": series["T"].get(h), "rh": series["H"].get(h),
               "wind": series["F"].get(h), "precip": None}
        parts = [series["SRA10M"].get(h - timedelta(minutes=m)) for m in range(0, 60, 10)]
        if all(p is not None for p in parts):
            rec["precip"] = round(sum(parts), 2)
        out[iso(h)] = rec
    return out


def fetch_observations(now):
    obs = {}
    for day in (now - timedelta(days=1), now):
        url = ("https://opendata.chmi.cz/meteorology/climate/now/data/"
               f"10m-{config.CHMI_STATION_WSI}-{day:%Y%m%d}.json")
        obs.update(parse_chmi_10m(http_json(url)))
    return obs


# --------------------------------------------------------------- ukládání

def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return str(v)


def append_csv(path: Path, fields, rows):
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: fmt(r.get(k)) for k in fields})


def upsert_observations(new_obs: dict):
    existing = {}
    if OBS_FILE.exists():
        with OBS_FILE.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing[r["valid_utc"]] = {k: num(r[k]) for k in OBS_FIELDS[1:]}
    for t, rec in new_obs.items():
        cur = existing.get(t, {})
        # Novější data přepíšou starší jen tam, kde nejsou prázdná.
        merged = {k: rec.get(k) if rec.get(k) is not None else cur.get(k)
                  for k in OBS_FIELDS[1:]}
        existing[t] = merged
    OBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OBS_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OBS_FIELDS)
        w.writeheader()
        for t in sorted(existing):
            w.writerow({"valid_utc": t, **{k: fmt(v) for k, v in existing[t].items()}})
    return len(new_obs)


# ------------------------------------------------------------------ běh

def main():
    now = datetime.now(UTC)
    issued = now.replace(minute=0, second=0, microsecond=0)
    run_log, all_rows = [], []

    for name, fn in providers():
        t0 = time.monotonic()
        try:
            rows = fn(issued)
            ok, err = True, ""
        except Exception as e:
            rows, ok, err = [], False, f"{type(e).__name__}: {e}"[:200]
        ms = int((time.monotonic() - t0) * 1000)
        if ok and not rows:
            ok, err = False, "prázdná odpověď"
        all_rows.extend(rows)
        run_log.append({"run_utc": iso(now), "provider": name, "ok": int(ok),
                        "rows": len(rows), "latency_ms": ms, "error": err})
        print(f"{name:16s} {'OK ' if ok else 'ERR'} {len(rows):5d} řádků {ms:6d} ms {err}")

    t0 = time.monotonic()
    try:
        n = upsert_observations(fetch_observations(now))
        ok, err = True, ""
    except Exception as e:
        n, ok, err = 0, False, f"{type(e).__name__}: {e}"[:200]
    ms = int((time.monotonic() - t0) * 1000)
    run_log.append({"run_utc": iso(now), "provider": "chmi_observations", "ok": int(ok),
                    "rows": n, "latency_ms": ms, "error": err})
    print(f"{'ČHMÚ měření':16s} {'OK ' if ok else 'ERR'} {n:5d} hodin  {ms:6d} ms {err}")

    append_csv(FORECAST_DIR / f"{issued:%Y-%m-%d}.csv", FORECAST_FIELDS, all_rows)
    append_csv(RUNS_FILE, RUN_FIELDS, run_log)

    if not any(r["ok"] for r in run_log):
        sys.exit("Nic se nepodařilo stáhnout.")


if __name__ == "__main__":
    main()
