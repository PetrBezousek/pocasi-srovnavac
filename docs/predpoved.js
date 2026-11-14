/* Předpověď složená z nejlepších modelů podle vyhodnocení (results.json).
 *
 * Pro každou hodinu a veličinu se vezme poskytovatel, který pro daný
 * předstih vyšel v analýze nejlépe; když pro tu hodinu data nemá,
 * použije se další v pořadí. U teploty a větru se odečte jeho naměřená
 * soustavná chyba (bias). Za 72 h se použije pořadí pro 49–72 h a hodiny
 * se označí jako neověřené.
 *
 * Soubor funguje v prohlížeči (window.Predpoved) i v Node (testy).
 */
(function (root) {
  "use strict";
  const H = 3600e3;
  const BUCKETS = [["1-24", 1, 24], ["25-48", 25, 48], ["49-72", 49, 72]];
  const DEFAULT_ORDER = ["brightsky", "openmeteo_best_match", "metno"];
  const MIN_BIAS = 0.1;      // menší soustavnou chybu neopravovat

  function bucketOf(lead) {
    for (const [name, , hi] of BUCKETS) if (lead <= hi) return name;
    return null;             // za 72 h
  }

  function series() { return {temp: new Map(), wind: new Map(), precip: new Map(), cloud: new Map(), code: new Map(), isDay: new Map()}; }
  const hourMs = t => Math.round(t / H) * H;
  const utc = s => Date.parse(/Z$|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z");

  // ---------------------------------------------------------- parsery
  function parseOpenMeteo(json, models) {
    const h = json.hourly || {}, out = {};
    const times = (h.time || []).map(utc);
    for (const m of models) {
      const get = v => h[`${v}_${m}`] || (models.length === 1 ? h[v] : null) || [];
      const s = series();
      const cols = {temp: get("temperature_2m"), wind: get("wind_speed_10m"), precip: get("precipitation"),
                    cloud: get("cloud_cover"), code: get("weather_code"), isDay: get("is_day")};
      times.forEach((t, i) => { for (const k in cols) { const v = cols[k][i]; if (v != null) s[k].set(t, v); } });
      out[`openmeteo_${m}`] = s;
    }
    return out;
  }

  function parseMetno(json) {
    const s = series();
    for (const step of (json.properties || {}).timeseries || []) {
      const t = utc(step.time), d = step.data || {}, inst = (d.instant || {}).details || {};
      if (inst.air_temperature != null) s.temp.set(t, inst.air_temperature);
      if (inst.wind_speed != null) s.wind.set(t, inst.wind_speed);
      if (inst.cloud_area_fraction != null) s.cloud.set(t, inst.cloud_area_fraction);
      const n1 = ((d.next_1_hours || {}).details || {}).precipitation_amount;
      const n6 = ((d.next_6_hours || {}).details || {}).precipitation_amount;
      if (n1 != null) s.precip.set(t + H, n1);                       // úhrn končí o hodinu později
      else if (n6 != null) for (let k = 1; k <= 6; k++)              // 6h úhrn rozložit do hodin
        if (!s.precip.has(t + k * H)) s.precip.set(t + k * H, n6 / 6);
    }
    return {metno: s};
  }

  function parseBrightsky(json) {
    const s = series();
    const fc = new Set((json.sources || []).filter(x => x.observation_type === "forecast").map(x => x.id));
    for (const w of json.weather || []) {
      if (fc.size && !fc.has(w.source_id)) continue;
      const t = utc(w.timestamp);
      if (w.temperature != null) s.temp.set(t, w.temperature);
      if (w.wind_speed != null) s.wind.set(t, w.wind_speed / 3.6);  // km/h -> m/s
      if (w.precipitation != null) s.precip.set(t, w.precipitation); // úhrn za předchozí hodinu
      if (w.cloud_cover != null) s.cloud.set(t, w.cloud_cover);
    }
    return {brightsky: s};
  }

  // ---------------------------------------------------------- výběr
  function orderFor(choice, bucket, v) {
    const list = ((choice || {})[bucket] || {})[v] || [];
    const ids = list.map(c => c.provider);
    return ids.length ? [...ids, ...DEFAULT_ORDER.filter(p => !ids.includes(p))] : DEFAULT_ORDER;
  }

  function biasOf(choice, bucket, v, provider) {
    const c = (((choice || {})[bucket] || {})[v] || []).find(x => x.provider === provider);
    return c && Math.abs(c.bias) >= MIN_BIAS ? c.bias : 0;
  }

  /** Které poskytovatele je potřeba stáhnout (nejlepší tři pro každou veličinu a předstih). */
  function neededProviders(choice) {
    const need = new Set(["openmeteo_best_match"]);       // ikony počasí a oblačnost
    for (const [b] of BUCKETS) for (const v of ["temp", "wind", "precip"])
      orderFor(choice, b, v).slice(0, 3).forEach(p => need.add(p));
    return [...need].filter(p => p.startsWith("openmeteo_") || p === "metno" || p === "brightsky");
  }

  /** Složí hodinovou předpověď. data = {provider: series}, now = ms. */
  function blend(data, choice, now, hours = 168) {
    const start = Math.ceil(now / H) * H, out = [];
    const icon = data.openmeteo_best_match || series();
    for (let t = start; t < start + hours * H; t += H) {
      const lead = Math.round((t - now) / H) || 1;
      const b = bucketOf(lead) || "49-72";
      const row = {t, lead, verified: lead <= 72, src: {}};
      for (const v of ["temp", "wind", "precip"]) {
        for (const p of orderFor(choice, b, v)) {
          const s = data[p]; if (!s || !s[v].has(t)) continue;
          let val = s[v].get(t);
          if (v !== "precip") val -= biasOf(choice, b, v, p);
          if (v !== "temp") val = Math.max(0, val);
          row[v] = val; row.src[v] = p; break;
        }
      }
      row.cloud = icon.cloud.get(t); row.code = icon.code.get(t);
      row.isDay = icon.isDay.has(t) ? icon.isDay.get(t) === 1 : localHour(t) >= 7 && localHour(t) < 17;
      if (row.temp == null && row.precip == null) continue;
      out.push(row);
    }
    return out;
  }

  const TZ = "Europe/Prague";
  function localHour(t) { return +new Intl.DateTimeFormat("en-GB", {timeZone: TZ, hour: "2-digit", hourCycle: "h23"}).format(t); }
  function localDay(t) { return new Intl.DateTimeFormat("sv-SE", {timeZone: TZ}).format(t); }

  function daily(rows) {
    const days = new Map();
    for (const r of rows) {
      const k = localDay(r.t);
      if (!days.has(k)) days.set(k, []);
      days.get(k).push(r);
    }
    return [...days].map(([day, rs]) => {
      const temps = rs.map(r => r.temp).filter(v => v != null);
      const pr = rs.map(r => r.precip).filter(v => v != null);
      const winds = rs.map(r => r.wind).filter(v => v != null);
      const dayRows = rs.filter(r => r.isDay);
      const mid = (dayRows.length ? dayRows : rs)[Math.floor((dayRows.length ? dayRows : rs).length / 2)];
      const wet = rs.filter(r => (r.precip || 0) >= 0.1).length;
      return {
        day, hours: rs.length, verified: rs.every(r => r.verified),
        tmin: temps.length ? Math.min(...temps) : null, tmax: temps.length ? Math.max(...temps) : null,
        precip: pr.reduce((a, b) => a + b, 0), wetHours: wet,
        windMax: winds.length ? Math.max(...winds) : null,
        icon: dayIcon(rs, mid),
      };
    });
  }

  // ---------------------------------------------------------- ikony
  // Srážky z vybraného modelu mají přednost před ikonou Open-Meteo, aby
  // ikona neukazovala déšť, když nejlepší srážkový model říká sucho.
  function iconFor(r) {
    const p = r.precip || 0, code = r.code;
    if (p >= 0.2) {
      if (r.temp != null && r.temp <= 0.5) return {e: "🌨️", t: "sněžení"};
      if (code >= 95) return {e: "⛈️", t: "bouřka"};
      return p >= 2 ? {e: "🌧️", t: "silný déšť"} : {e: "🌦️", t: "déšť"};
    }
    if (code === 45 || code === 48) return {e: "🌫️", t: "mlha"};
    const c = r.cloud;
    if (c == null) return {e: "·", t: ""};
    if (c < 25) return r.isDay ? {e: "☀️", t: "jasno"} : {e: "🌙", t: "jasno"};
    if (c < 70) return r.isDay ? {e: "⛅", t: "polojasno"} : {e: "☁️", t: "polojasno"};
    return {e: "☁️", t: "zataženo"};
  }

  function dayIcon(rs, mid) {
    const dayRows = rs.filter(r => r.isDay);
    const sum = rs.reduce((a, r) => a + (r.precip || 0), 0);
    const temps = rs.map(r => r.temp).filter(v => v != null);
    if (sum >= 1) {
      const cold = temps.length && Math.max(...temps) <= 1;
      return cold ? {e: "🌨️", t: "sněžení"} : sum >= 8 ? {e: "🌧️", t: "deštivo"} : {e: "🌦️", t: "přeháňky"};
    }
    const clouds = (dayRows.length ? dayRows : rs).map(r => r.cloud).filter(v => v != null);
    const fog = (dayRows.length ? dayRows : rs).filter(r => r.code === 45 || r.code === 48).length;
    if (fog >= 4) return {e: "🌫️", t: "mlhavo"};
    const avg = clouds.length ? clouds.reduce((a, b) => a + b, 0) / clouds.length : null;
    if (avg == null) return iconFor(mid);
    return avg < 30 ? {e: "☀️", t: "slunečno"} : avg < 70 ? {e: "⛅", t: "polojasno"} : {e: "☁️", t: "zataženo"};
  }

  // ---------------------------------------------------------- stahování
  async function fetchAll(choice, lat, lon, now, fetchFn) {
    const f = fetchFn || fetch.bind(root);
    const need = neededProviders(choice);
    const om = need.filter(p => p.startsWith("openmeteo_")).map(p => p.slice(10));
    const errors = {}, data = {};
    const jobs = [];
    if (om.length) {
      const q = new URLSearchParams({latitude: lat, longitude: lon, timezone: "GMT", forecast_days: 8,
        wind_speed_unit: "ms", models: om.join(","),
        hourly: "temperature_2m,precipitation,wind_speed_10m,cloud_cover,weather_code,is_day"});
      jobs.push(f("https://api.open-meteo.com/v1/forecast?" + q).then(okJson)
        .then(j => Object.assign(data, parseOpenMeteo(j, om))).catch(e => { errors.openmeteo = String(e); }));
    }
    if (need.includes("metno"))
      jobs.push(f(`https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=${(+lat).toFixed(4)}&lon=${(+lon).toFixed(4)}`)
        .then(okJson).then(j => Object.assign(data, parseMetno(j))).catch(e => { errors.metno = String(e); }));
    if (need.includes("brightsky")) {
      const from = new Date(Math.floor(now / H) * H).toISOString().slice(0, 16);
      const to = new Date(now + 8 * 24 * H).toISOString().slice(0, 16);
      const q = new URLSearchParams({lat, lon, date: from, last_date: to, tz: "Etc/UTC", max_dist: 60000});
      jobs.push(f("https://api.brightsky.dev/weather?" + q).then(okJson)
        .then(j => Object.assign(data, parseBrightsky(j))).catch(e => { errors.brightsky = String(e); }));
    }
    await Promise.all(jobs);
    return {data, errors};
  }

  function okJson(r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }

  const api = {BUCKETS, bucketOf, parseOpenMeteo, parseMetno, parseBrightsky, neededProviders,
               blend, daily, iconFor, fetchAll, localDay, localHour, orderFor, biasOf};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Predpoved = api;
})(typeof window !== "undefined" ? window : globalThis);
