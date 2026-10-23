"""Vyhodnocení přesnosti předpovědí proti měření ČHMÚ.

Načte data/forecasts/*.csv, data/observations.csv a data/runs.csv
a zapíše docs/results.json, ze kterého čte dashboard.

Metriky:
  teplota, vlhkost, vítr  MAE (průměrná absolutní chyba), bias, RMSE
  srážky                  6hodinová okna; trefa ano/ne (práh 0,5 mm),
                          POD (zachycené srážky), FAR (plané poplachy), MAE úhrnu
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "results.json"
UTC = timezone.utc

BUCKETS = [("1-24", 1, 24), ("25-48", 25, 48), ("49-72", 49, 72)]
PRECIP_WINDOW_H = 6
PRECIP_THRESHOLD = 0.5      # mm za 6 h = "pršelo"
MIN_N_HOURLY = 24           # minimum párů pro zařazení do žebříčku
MIN_N_PRECIP = 8
WEIGHTS = {"temp": 0.4, "precip": 0.3, "wind": 0.15, "rh": 0.15}


def t(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v):
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except ValueError:
        return None


def bucket_of(lead):
    for name, lo, hi in BUCKETS:
        if lo <= lead <= hi:
            return name
    return None


def label(pid: str) -> str:
    if pid.startswith("openmeteo_"):
        return config.OPEN_METEO_MODELS.get(pid[len("openmeteo_"):], pid)
    return config.PROVIDER_LABELS.get(pid, pid)


# ------------------------------------------------------------ načtení

def load_obs():
    obs = {}
    p = DATA / "observations.csv"
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                obs[t(r["valid_utc"])] = {k: num(r[k]) for k in ("temp", "rh", "wind", "precip")}
    return obs


def load_forecasts():
    rows = []
    for p in sorted((DATA / "forecasts").glob("*.csv")):
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "provider": r["provider"], "issued": t(r["issued_utc"]),
                    "valid": t(r["valid_utc"]), "lead": int(r["lead_h"]),
                    "temp": num(r["temp"]), "rh": num(r["rh"]), "wind": num(r["wind"]),
                    "precip": num(r["precip"]),
                    "precip_h": int(float(r["precip_h"])) if r["precip_h"] else None,
                })
    return rows


def load_runs():
    p = DATA / "runs.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------ metriky

def cont_stats(errors):
    if not errors:
        return {"n": 0}
    n = len(errors)
    return {
        "n": n,
        "mae": round(sum(abs(e) for e in errors) / n, 3),
        "bias": round(sum(errors) / n, 3),
        "rmse": round(math.sqrt(sum(e * e for e in errors) / n), 3),
    }


def precip_stats(pairs):
    """pairs = [(forecast_mm, observed_mm)]"""
    if not pairs:
        return {"n": 0}
    hit = miss = fa = cn = 0
    for fc, ob in pairs:
        f_yes, o_yes = fc >= PRECIP_THRESHOLD, ob >= PRECIP_THRESHOLD
        if f_yes and o_yes:
            hit += 1
        elif o_yes:
            miss += 1
        elif f_yes:
            fa += 1
        else:
            cn += 1
    n = len(pairs)
    return {
        "n": n,
        "acc": round((hit + cn) / n, 3),
        "pod": round(hit / (hit + miss), 3) if hit + miss else None,
        "far": round(fa / (hit + fa), 3) if hit + fa else None,
        "mae": round(sum(abs(f - o) for f, o in pairs) / n, 3),
        "hits": hit, "misses": miss, "false_alarms": fa, "correct_neg": cn,
    }


def precip_windows(fc_rows, obs):
    """Vrátí {(provider, bucket): [(fc, ob)]} pro 6h okna končící v 00/06/12/18 UTC."""
    by_run = defaultdict(list)
    for r in fc_rows:
        if r["precip"] is not None and r["precip_h"]:
            by_run[(r["provider"], r["issued"])].append(r)
    out = defaultdict(list)
    W = PRECIP_WINDOW_H
    for (prov, issued), recs in by_run.items():
        by_end = defaultdict(lambda: [0.0, 0])
        for r in recs:
            start = r["valid"] - timedelta(hours=r["precip_h"])
            # konec okna, do kterého interval patří
            end_h = math.ceil(r["valid"].timestamp() / 3600 / W) * W
            end = datetime.fromtimestamp(end_h * 3600, UTC)
            if start >= end - timedelta(hours=W):
                acc = by_end[end]
                acc[0] += r["precip"]
                acc[1] += r["precip_h"]
        for end, (amount, hours) in by_end.items():
            if hours != W or end - timedelta(hours=W) < issued:
                continue
            ob_parts = [obs.get(end - timedelta(hours=k), {}).get("precip") for k in range(W)]
            if any(p is None for p in ob_parts):
                continue
            lead = round((end - issued).total_seconds() / 3600)
            b = bucket_of(lead)
            if b:
                out[(prov, b)].append((amount, sum(ob_parts)))
    return out


def rank_providers(metrics, providers):
    ranking = {}
    for bname, _, _ in BUCKETS:
        cand = {}
        for p in providers:
            m = metrics.get(p, {}).get(bname, {})
            vals = {}
            for var in ("temp", "wind", "rh"):
                s = m.get(var, {})
                if s.get("n", 0) >= MIN_N_HOURLY:
                    vals[var] = s["mae"]
            s = m.get("precip", {})
            if s.get("n", 0) >= MIN_N_PRECIP:
                vals["precip"] = -s["acc"]  # vyšší přesnost = lepší
            if "temp" in vals:
                cand[p] = vals
        ranks = {p: {} for p in cand}
        for var in WEIGHTS:
            have = sorted((v[var], p) for p, v in cand.items() if var in v)
            for i, (_, p) in enumerate(have):
                # remíza = stejné pořadí
                ranks[p][var] = 1 + sum(1 for x, _ in have if x < have[i][0])
        table = []
        for p, rk in ranks.items():
            w = sum(WEIGHTS[v] for v in rk)
            score = sum(WEIGHTS[v] * r for v, r in rk.items()) / w
            table.append({"provider": p, "score": round(score, 2), "ranks": rk})
        table.sort(key=lambda x: (x["score"], metrics[x["provider"]][bname]["temp"]["mae"]))
        ranking[bname] = table
    return ranking


# ------------------------------------------------------------ hlavní

def main():
    obs = load_obs()
    fc = load_forecasts()
    runs = load_runs()
    providers = sorted({r["provider"] for r in fc})

    errs = defaultdict(lambda: defaultdict(list))         # (prov, bucket) -> var -> [err]
    by_lead = defaultdict(lambda: defaultdict(list))      # prov -> lead_bin -> [abs temp err]
    daily = defaultdict(lambda: defaultdict(list))        # prov -> date -> [abs temp err] (1-24 h)
    for r in fc:
        b = bucket_of(r["lead"])
        o = obs.get(r["valid"])
        if not b or not o:
            continue
        for var in ("temp", "rh", "wind"):
            if r[var] is not None and o[var] is not None:
                errs[(r["provider"], b)][var].append(r[var] - o[var])
        if r["temp"] is not None and o["temp"] is not None:
            e = abs(r["temp"] - o["temp"])
            by_lead[r["provider"]][(r["lead"] - 1) // 6 * 6 + 6].append(e)
            if b == "1-24":
                daily[r["provider"]][r["valid"].strftime("%Y-%m-%d")].append(e)

    pw = precip_windows(fc, obs)
    metrics = {}
    for p in providers:
        metrics[p] = {}
        for bname, _, _ in BUCKETS:
            e = errs.get((p, bname), {})
            metrics[p][bname] = {var: cont_stats(e.get(var, [])) for var in ("temp", "rh", "wind")}
            metrics[p][bname]["precip"] = precip_stats(pw.get((p, bname), []))

    # dostupnost API
    avail = defaultdict(lambda: {"runs": 0, "ok": 0, "lat": [], "last_error": ""})
    for r in runs:
        a = avail[r["provider"]]
        a["runs"] += 1
        a["ok"] += int(r["ok"])
        a["lat"].append(int(r["latency_ms"]))
        if r["error"]:
            a["last_error"] = f'{r["run_utc"]}: {r["error"]}'
    availability = {p: {"runs": a["runs"], "ok": a["ok"],
                        "rate": round(a["ok"] / a["runs"], 3) if a["runs"] else None,
                        "latency_ms": int(statistics.median(a["lat"])) if a["lat"] else None,
                        "last_error": a["last_error"]}
                    for p, a in avail.items()}

    # poslední předpověď pro graf "co hlásí teď"
    latest = {}
    if fc:
        last_issued = max(r["issued"] for r in fc)
        latest = {"issued_utc": iso(last_issued), "forecasts": defaultdict(list),
                  "obs": [[iso(k), v["temp"]] for k, v in sorted(obs.items())
                          if k >= last_issued - timedelta(hours=48) and v["temp"] is not None]}
        for r in fc:
            if r["issued"] == last_issued and r["temp"] is not None:
                latest["forecasts"][r["provider"]].append([iso(r["valid"]), r["temp"]])
        latest["forecasts"] = {p: sorted(v) for p, v in latest["forecasts"].items()}

    result = {
        "generated_utc": iso(datetime.now(UTC)),
        "location": config.LOCATION_NAME,
        "station": config.CHMI_STATION_NAME,
        "period": {"first_issued": iso(min(r["issued"] for r in fc)) if fc else None,
                   "last_issued": iso(max(r["issued"] for r in fc)) if fc else None},
        "n_runs": len({r["issued"] for r in fc}),
        "n_obs_hours": sum(1 for v in obs.values() if v["temp"] is not None),
        "buckets": [b[0] for b in BUCKETS],
        "settings": {"precip_threshold_mm": PRECIP_THRESHOLD, "precip_window_h": PRECIP_WINDOW_H,
                     "min_n_hourly": MIN_N_HOURLY, "min_n_precip": MIN_N_PRECIP,
                     "weights": WEIGHTS},
        "providers": [{"id": p, "label": label(p)} for p in providers],
        "metrics": metrics,
        "ranking": rank_providers(metrics, providers),
        "temp_mae_by_lead": {p: [[k, round(sum(v) / len(v), 3), len(v)]
                                 for k, v in sorted(d.items())] for p, d in by_lead.items()},
        "daily_temp_mae": {p: [[k, round(sum(v) / len(v), 3), len(v)]
                               for k, v in sorted(d.items())] for p, d in daily.items()},
        "availability": availability,
        "latest": latest,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Hotovo: {len(fc)} předpovědí, {len(obs)} hodin měření, "
          f"{len(providers)} poskytovatelů -> {OUT}")


if __name__ == "__main__":
    main()
