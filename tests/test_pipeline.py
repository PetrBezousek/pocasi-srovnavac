"""Testy parserů (na vzorcích reálných odpovědí) a celého vyhodnocení.

Spuštění: python -m unittest discover tests
"""

import csv
import json
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402
import config  # noqa: E402
import evaluate  # noqa: E402

UTC = timezone.utc
ISSUED = datetime(2026, 10, 21, 18, tzinfo=UTC)


class ParserTests(unittest.TestCase):
    def test_chmi_10m(self):
        vals = []
        start = datetime(2026, 10, 21, 0, tzinfo=UTC)
        for i in range(13):  # 00:00 .. 02:00
            ts = collect.iso(start + timedelta(minutes=10 * i))
            vals += [["0-20000-0-11679", "T", ts, 10.0 + i / 10, "", 5.0],
                     ["0-20000-0-11679", "H", ts, 70.0, "", 5.0],
                     ["0-20000-0-11679", "F", ts, 5.2, "", 5.0],
                     ["0-20000-0-11679", "SRA10M", ts, 0.1, "", 5.0]]
        vals.append(["0-20000-0-11679", "SSV10M", collect.iso(start), "", "", 4.0])
        data = {"data": {"type": "DataCollection", "data": {
            "header": "STATION,ELEMENT,DT,VAL,FLAG,QUALITY", "values": vals}}}
        obs = collect.parse_chmi_10m(data)
        self.assertEqual(obs["2026-10-21T01:00:00Z"]["temp"], 10.6)
        self.assertEqual(obs["2026-10-21T01:00:00Z"]["precip"], 0.6)
        self.assertIsNone(obs["2026-10-21T00:00:00Z"]["precip"])  # chybí 23:10–23:50

    def test_open_meteo(self):
        times = [(ISSUED + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(-2, 80)]
        hourly = {"time": times}
        for m in config.OPEN_METEO_MODELS:
            hourly[f"temperature_2m_{m}"] = [12.0] * len(times)
            hourly[f"relative_humidity_2m_{m}"] = [80] * len(times)
            hourly[f"wind_speed_10m_{m}"] = [3.1] * len(times)
            hourly[f"precipitation_{m}"] = [0.2] * len(times)
        hourly["temperature_2m_icon_d2"] = [12.0] * 50 + [None] * (len(times) - 50)
        hourly["relative_humidity_2m_icon_d2"] = [80] * 50 + [None] * (len(times) - 50)
        hourly["wind_speed_10m_icon_d2"] = [3.1] * 50 + [None] * (len(times) - 50)
        hourly["precipitation_icon_d2"] = [0.2] * 50 + [None] * (len(times) - 50)
        rows = collect.parse_open_meteo({"hourly": hourly}, ISSUED)
        best = [r for r in rows if r["provider"] == "openmeteo_best_match"]
        d2 = [r for r in rows if r["provider"] == "openmeteo_icon_d2"]
        self.assertEqual(len(best), 72)
        self.assertEqual(best[0]["lead_h"], 1)
        self.assertEqual(len(d2), 47)  # model končí dřív

    def test_metno(self):
        ts = []
        for h in range(0, 61):
            ts.append({"time": collect.iso(ISSUED + timedelta(hours=h)), "data": {
                "instant": {"details": {"air_temperature": 14.6, "relative_humidity": 66.5,
                                        "wind_speed": 6.3}},
                "next_1_hours": {"details": {"precipitation_amount": 0.2}},
                "next_6_hours": {"details": {"precipitation_amount": 7.4}}}})
        for h in range(66, 80, 6):
            ts.append({"time": collect.iso(ISSUED + timedelta(hours=h)), "data": {
                "instant": {"details": {"air_temperature": 5.6, "relative_humidity": 72.9,
                                        "wind_speed": 1.8}},
                "next_6_hours": {"details": {"precipitation_amount": 1.2}}}})
        rows = collect.parse_metno({"properties": {"timeseries": ts}}, ISSUED)
        r1 = next(r for r in rows if r["lead_h"] == 1)
        self.assertEqual((r1["temp"], r1["precip"], r1["precip_h"]), (14.6, 0.2, 1))
        r72 = next(r for r in rows if r["lead_h"] == 72)
        self.assertEqual((r72["temp"], r72["precip"], r72["precip_h"]), (5.6, 1.2, 6))

    def test_brightsky(self):
        data = {"weather": [{"timestamp": "2026-10-22T00:00:00+00:00", "source_id": 2998,
                             "precipitation": 2.4, "temperature": 11.7, "wind_speed": 13.0,
                             "dew_point": 7.7, "relative_humidity": None}],
                "sources": [{"id": 2998, "observation_type": "forecast"}]}
        r = collect.parse_brightsky(data, ISSUED)[0]
        self.assertEqual(r["lead_h"], 6)
        self.assertAlmostEqual(r["wind"], 13.0 / 3.6)
        self.assertTrue(75 < r["rh"] < 80)

    def test_openweathermap(self):
        data = {"list": [{"dt": int((ISSUED + timedelta(hours=3)).timestamp()),
                          "main": {"temp": 11.0, "humidity": 77}, "wind": {"speed": 4.0},
                          "rain": {"3h": 1.5}}]}
        r = collect.parse_openweathermap(data, ISSUED)[0]
        self.assertEqual((r["lead_h"], r["precip"], r["precip_h"]), (3, 1.5, 3))

    def test_weatherapi(self):
        hours = [{"time_epoch": int((ISSUED + timedelta(hours=h)).timestamp()), "temp_c": 10,
                  "humidity": 70, "wind_kph": 18, "precip_mm": 0.3} for h in range(0, 5)]
        rows = collect.parse_weatherapi({"forecast": {"forecastday": [{"hour": hours}]}}, ISSUED)
        r = next(x for x in rows if x["lead_h"] == 1)
        self.assertEqual((r["temp"], r["wind"], r["precip"]), (10, 5.0, 0.3))


class EvaluateTests(unittest.TestCase):
    def test_end_to_end_ranking(self):
        """Poskytovatel s menším šumem musí skončit první."""
        rnd = random.Random(1)
        tmp = Path(tempfile.mkdtemp())
        (tmp / "forecasts").mkdir()
        evaluate.DATA, evaluate.OUT = tmp, tmp / "results.json"

        start = datetime(2026, 10, 1, tzinfo=UTC)
        truth = {}
        with (tmp / "observations.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["valid_utc", "temp", "rh", "wind", "precip"])
            for h in range(24 * 12):
                t = start + timedelta(hours=h)
                rain = 1.0 if (h // 6) % 4 == 0 else 0.0
                truth[t] = (10 + 5 * ((h % 24) / 24), 70.0, 4.0, rain)
                w.writerow([collect.iso(t), *truth[t]])

        noise = {"good": 0.5, "medium": 1.5, "bad": 3.0}
        with (tmp / "forecasts" / "x.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=collect.FORECAST_FIELDS)
            w.writeheader()
            for run in range(0, 24 * 9, 3):
                issued = start + timedelta(hours=run)
                for prov, sd in noise.items():
                    for lead in range(1, 73):
                        v = issued + timedelta(hours=lead)
                        tt, rh, ws, pr = truth[v]
                        flip = rnd.random() < sd / 6
                        w.writerow({"issued_utc": collect.iso(issued), "provider": prov,
                                    "valid_utc": collect.iso(v), "lead_h": lead,
                                    "temp": round(tt + rnd.gauss(0, sd), 2),
                                    "rh": round(rh + rnd.gauss(0, sd * 3), 1),
                                    "wind": round(ws + rnd.gauss(0, sd / 2), 2),
                                    "precip": (1.0 - pr) if flip else pr, "precip_h": 1})
        with (tmp / "runs.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(collect.RUN_FIELDS)
            w.writerow(["2026-10-01T00:17:00Z", "good", 1, 72, 300, ""])
            w.writerow(["2026-10-01T00:17:00Z", "bad", 0, 0, 900, "timeout"])

        evaluate.main()
        res = json.loads((tmp / "results.json").read_text())
        for b in res["buckets"]:
            order = [r["provider"] for r in res["ranking"][b]]
            self.assertEqual(order, ["good", "medium", "bad"], b)
        m = res["metrics"]["good"]["1-24"]
        self.assertLess(m["temp"]["mae"], 0.6)
        self.assertGreater(m["precip"]["n"], 20)
        self.assertGreater(m["precip"]["acc"], res["metrics"]["bad"]["1-24"]["precip"]["acc"])
        self.assertEqual(res["availability"]["bad"]["rate"], 0)


if __name__ == "__main__":
    unittest.main()
