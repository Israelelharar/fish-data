// Source watchdog: "how do we know the official sources didn't change?"
// Fetches every official page our rules rely on, fingerprints its visible text, and compares with the
// last run. If a page changed (new season dates, new list, moved page / 404), it writes changes.json and
// exits with code 2, so the GitHub workflow opens an issue asking a human to review and update the rules.
//
// Usage: node scripts/check-sources.mjs site/sources.json [previous-url]
import { createHash } from "node:crypto";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { readFileSync } from "node:fs";
const BUNDLED_RULES = JSON.parse(readFileSync(new URL("../data/rules.json", import.meta.url), "utf8")).rules;

const out = process.argv[2] ?? "out-data/sources.json";
const prevUrl = process.argv[3];
const EXTRA = [
  "https://www.parks.org.il/article/border-nature/", // reserve coordinates
  "https://www.parks.org.il/article/fish-tk/", // anglers booklet (minimum sizes)
  "https://www.parks.org.il/article/eilat-fishing/",
];
const urls = [...new Set([...BUNDLED_RULES.map((r) => r.source), ...EXTRA])];

const text = (html) =>
  html
    .replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>|<nav[\s\S]*?<\/nav>|<footer[\s\S]*?<\/footer>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    // ignore "related news" blocks and dates that change daily
    .replace(/אולי יעניין אותך גם[\s\S]*/g, "")
    .replace(/\s+/g, " ")
    .trim();

const now = {};
for (const url of urls) {
  try {
    const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (dayag-hacham source monitor)" } });
    const body = r.ok ? (url.endsWith(".pdf") ? Buffer.from(await r.arrayBuffer()).toString("base64") : text(await r.text())) : "";
    now[url] = { status: r.status, hash: createHash("sha256").update(body).digest("hex").slice(0, 16) };
  } catch (e) {
    now[url] = { status: 0, hash: "", error: String(e).slice(0, 80) };
  }
  await new Promise((s) => setTimeout(s, 500));
}

let prev = {};
if (prevUrl) {
  try {
    prev = (await (await fetch(prevUrl)).json()).pages ?? {};
  } catch { /* first run */ }
}
const changes = Object.entries(now)
  .filter(([u, v]) => v.status !== 200 || (prev[u] && prev[u].hash !== v.hash))
  .map(([u, v]) => ({ url: u, status: v.status, was: prev[u]?.hash ?? null, now: v.hash }));

mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, JSON.stringify({ checked: new Date().toISOString(), pages: now, changes }, null, 1));
console.log(`checked ${urls.length} sources, ${changes.length} changed/broken`);
if (changes.length) {
  writeFileSync(dirname(out) + "/changes.md", "Official sources changed or became unreachable. Review and update the rules in the app (regulations / fish sizes / reserves), then re-export data/rules.json:\n\n" + changes.map((c) => `- ${c.url} (HTTP ${c.status})`).join("\n"));
  process.exitCode = 2;
}
