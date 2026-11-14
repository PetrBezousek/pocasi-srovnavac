// Testy logiky stránky s předpovědí. Spuštění: node tests/test_predpoved.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const P = require("../docs/predpoved.js");

const H = 3600e3;
const now = Date.parse("2026-11-14T21:30:00Z");
const iso = t => new Date(t).toISOString().slice(0, 16);   // jako Open-Meteo (bez Z)

const choice = {
  "1-24":  {temp: [{provider: "brightsky", mae: 0.85, bias: -0.1, n: 900}, {provider: "openmeteo_icon_d2", mae: 0.92, bias: 0.3, n: 900}],
            wind: [{provider: "openmeteo_icon_d2", mae: 0.74, bias: -0.28, n: 900}],
            precip: [{provider: "openmeteo_gem_seamless", csi: 0.56, acc: 0.93, n: 130}]},
  "25-48": {temp: [{provider: "openmeteo_icon_d2", mae: 1.1, bias: 0.5, n: 900}], wind: [], precip: []},
  "49-72": {temp: [{provider: "openmeteo_ecmwf_aifs025_single", mae: 1.2, bias: 0.05, n: 900}], wind: [], precip: [{provider: "metno", csi: 0.48, acc: 0.9, n: 140}]},
};

// --- bucket
assert.equal(P.bucketOf(1), "1-24"); assert.equal(P.bucketOf(25), "25-48"); assert.equal(P.bucketOf(72), "49-72"); assert.equal(P.bucketOf(73), null);

// --- potřební poskytovatelé
const need = P.neededProviders(choice);
for (const p of ["openmeteo_best_match", "brightsky", "openmeteo_icon_d2", "openmeteo_gem_seamless", "metno", "openmeteo_ecmwf_aifs025_single"])
  assert.ok(need.includes(p), p);

// --- Open-Meteo: ICON-D2 končí po 48 h, ostatní jedou 8 dní
const times = []; for (let t = now - 2 * H; t < now + 8 * 24 * H; t += H) times.push(Math.floor(t / H) * H);
const models = ["best_match", "icon_d2", "gem_seamless", "ecmwf_aifs025_single"];
const hourly = {time: times.map(iso)};
for (const m of models) {
  hourly[`temperature_2m_${m}`] = times.map(t => m === "icon_d2" && t > now + 48 * H ? null : 10);
  hourly[`precipitation_${m}`] = times.map(() => m === "gem_seamless" ? 0.5 : 0);
  hourly[`wind_speed_10m_${m}`] = times.map(() => 3);
  hourly[`cloud_cover_${m}`] = times.map(() => 50);
  hourly[`weather_code_${m}`] = times.map(() => 3);
  hourly[`is_day_${m}`] = times.map(t => 1);
}
const om = P.parseOpenMeteo({hourly}, models);
assert.equal(om.openmeteo_icon_d2.temp.size, times.filter(t => t <= now + 48 * H).length);

// --- Bright Sky jen 30 h, rychlost v km/h
const bs = P.parseBrightsky({sources: [{id: 1, observation_type: "forecast"}],
  weather: times.filter(t => t < now + 30 * H).map(t => ({timestamp: new Date(t).toISOString(), source_id: 1, temperature: 8, wind_speed: 18, precipitation: 0, cloud_cover: 90}))});
assert.equal(bs.brightsky.wind.get(times[5]), 5);

// --- MET Norway: hodinové a 6h srážky
const mt = P.parseMetno({properties: {timeseries: [
  {time: "2026-11-17T00:00:00Z", data: {instant: {details: {air_temperature: 2, wind_speed: 1}}, next_1_hours: {details: {precipitation_amount: 0.4}}}},
  {time: "2026-11-18T00:00:00Z", data: {instant: {details: {air_temperature: 1, wind_speed: 1}}, next_6_hours: {details: {precipitation_amount: 3.0}}}},
]}});
assert.equal(mt.metno.precip.get(Date.parse("2026-11-17T01:00:00Z")), 0.4);
assert.equal(mt.metno.precip.get(Date.parse("2026-11-18T03:00:00Z")), 0.5);

// --- skládání
const rows = P.blend({...om, ...bs, ...mt}, choice, now);
assert.equal(rows.length, 168);
const r1 = rows[0];
assert.equal(r1.lead, 1);
assert.equal(r1.src.temp, "brightsky"); assert.ok(Math.abs(r1.temp - 8.1) < 1e-9);       // 8 − (−0,1)
assert.equal(r1.src.wind, "openmeteo_icon_d2"); assert.ok(Math.abs(r1.wind - 3.28) < 1e-9); // 3 − (−0,28)
assert.equal(r1.src.precip, "openmeteo_gem_seamless"); assert.equal(r1.precip, 0.5);
const r30 = rows.find(r => r.lead === 31);                 // 25–48: ICON-D2 s opravou +0,5
assert.equal(r30.src.temp, "openmeteo_icon_d2"); assert.ok(Math.abs(r30.temp - 9.5) < 1e-9);
const r60 = rows.find(r => r.lead === 60);
assert.equal(r60.src.temp, "openmeteo_ecmwf_aifs025_single"); assert.equal(r60.temp, 10);  // bias 0,05 < 0,1 -> bez opravy
const r100 = rows.find(r => r.lead === 100);
assert.equal(r100.verified, false); assert.equal(r100.src.temp, "openmeteo_ecmwf_aifs025_single");
// 25–48 nemá pořadí pro srážky -> výchozí pořadí (Bright Sky už nemá data -> best_match)
assert.equal(rows.find(r => r.lead === 40).src.precip, "openmeteo_best_match");

// --- bez výsledků analýzy: výchozí pořadí
assert.equal(P.blend({...om, ...bs}, {}, now)[0].src.temp, "brightsky");

// --- denní souhrn
const days = P.daily(rows);
assert.ok(days.length >= 7 && days.length <= 9);
assert.ok(days[1].tmin <= days[1].tmax);
assert.equal(days[0].verified, true); assert.equal(days.at(-1).verified, false);

// --- ikony: vybraný srážkový model má přednost
assert.equal(P.iconFor({precip: 0, cloud: 10, isDay: true, code: 61}).t, "jasno");
assert.equal(P.iconFor({precip: 1, temp: -1, cloud: 90}).t, "sněžení");

// --- stahování s výpadkem jednoho zdroje
const fake = async url => {
  if (url.includes("met.no")) return {ok: false, status: 503};
  if (url.includes("open-meteo")) return {ok: true, json: async () => ({hourly})};
  return {ok: true, json: async () => ({sources: [], weather: []})};
};
const res = await P.fetchAll(choice, 49.98, 16.42, now, fake);
assert.ok(res.errors.metno.includes("503")); assert.ok(res.data.openmeteo_icon_d2);
console.log("OK – všechny testy předpovědi prošly");
