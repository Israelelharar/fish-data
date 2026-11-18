// Daily reality check: how accurate was the forecast?
// For each coastal IMS station: compare the MET Norway forecast for the current hour with what the
// station actually measured (wind, gust, temperature), and append the errors to accuracy.json.
// Over weeks this shows which source is most accurate where, and lets us correct systematic bias
// (e.g. "forecast under-estimates gusts at Haifa by 20%").
//
// Run by .github/workflows/data.yml. Usage: node scripts/verify-accuracy.mjs site/accuracy.json [previous-url]
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const out = process.argv[2] ?? "out-data/accuracy.json";
const prevUrl = process.argv[3];
const UA = process.env.MET_USER_AGENT || "dayag-hacham/1.2 accuracy-check (github.com/Israelelharar/fish-data)";

const STATIONS = [
  ["ROSH HANIQRA", 33.077, 35.111], ["SHAVE ZIYYON", 32.984, 35.09], ["HAIFA REFINERIES", 32.806, 35.055],
  ["HADERA PORT", 32.473, 34.881], ["TEL AVIV COAST", 32.058, 34.759], ["ASHDOD PORT", 31.835, 34.645],
  ["ASHQELON PORT", 31.645, 34.523], ["ELAT", 29.553, 34.952], ["ZEMAH", 32.705, 35.584], ["LEV KINERET", 32.82, 35.59],
];

const xml = await (await fetch("https://ims.gov.il/sites/default/files/ims_data/xml_files/imslasthour.xml", { headers: { "User-Agent": UA } })).text();
const latest = new Map();
for (const m of xml.matchAll(/<Observation>([\s\S]*?)<\/Observation>/g)) {
  const tag = (t) => m[1].match(new RegExp(`<${t}>([^<]*)</${t}>`))?.[1];
  const name = tag("stn_name")?.trim();
  const t = tag("time_obs");
  if (!name || !t) continue;
  if (!latest.has(name) || latest.get(name).t < t) latest.set(name, { t, ws: Number(tag("WS")), gust: Number(tag("WSmax")), td: Number(tag("TD")) });
}

const rows = [];
for (const [name, lat, lon] of STATIONS) {
  const o = latest.get(name);
  if (!o) continue;
  const r = await fetch(`https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=${lat}&lon=${lon}`, { headers: { "User-Agent": UA } });
  if (!r.ok) continue;
  const ts = (await r.json()).properties.timeseries;
  const obsMs = Date.parse(o.t + "+02:00"); // approx (IMS local time); good enough to pick the nearest hour
  const f = ts.reduce((a, b) => (Math.abs(Date.parse(b.time) - obsMs) < Math.abs(Date.parse(a.time) - obsMs) ? b : a));
  const d = f.data.instant.details;
  rows.push({
    station: name, obs: o.t,
    windObs: +(o.ws * 3.6).toFixed(1), windFc: +(d.wind_speed * 3.6).toFixed(1),
    gustObs: +(o.gust * 3.6).toFixed(1),
    tempObs: o.td, tempFc: d.air_temperature,
  });
  await new Promise((s) => setTimeout(s, 300));
}

let history = [];
if (prevUrl) {
  try {
    history = (await (await fetch(prevUrl)).json()).history ?? [];
  } catch { /* first run */ }
}
history.push({ at: new Date().toISOString(), rows });
history = history.slice(-24 * 60); // keep ~60 days of runs

const all = history.flatMap((h) => h.rows);
const mae = (k1, k2) => {
  const ok = all.filter((r) => Number.isFinite(r[k1]) && Number.isFinite(r[k2]));
  return ok.length ? +(ok.reduce((s, r) => s + Math.abs(r[k1] - r[k2]), 0) / ok.length).toFixed(2) : null;
};
const summary = { samples: all.length, windBiasKmh: (() => { const ok = all.filter((r) => Number.isFinite(r.windObs) && Number.isFinite(r.windFc)); return ok.length ? +(ok.reduce((s, r) => s + (r.windFc - r.windObs), 0) / ok.length).toFixed(2) : null; })(), windMaeKmh: mae("windObs", "windFc"), tempMaeC: mae("tempObs", "tempFc") };
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, JSON.stringify({ updated: new Date().toISOString(), summary, history }));
console.log("accuracy", JSON.stringify(summary), "this run:", rows.length, "stations");
