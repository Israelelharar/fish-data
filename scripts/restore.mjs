// Starts every run from the files that are live right now. A source that fails this run then
// keeps serving its last good file instead of disappearing (the app also ignores files > 36 h old).
// Usage: node scripts/restore.mjs site
import { mkdirSync, writeFileSync } from "node:fs";

const dir = process.argv[2] ?? "site";
const BASE = process.env.BASE ?? "https://israelelharar.github.io/fish-data";
const FILES = ["accuracy.json", "sources.json", "marine.json", "gusts.json", "status.json"];
mkdirSync(dir, { recursive: true });

for (const f of FILES) {
  try {
    const res = await fetch(`${BASE}/${f}?t=${Date.now()}`, { signal: AbortSignal.timeout(30_000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    JSON.parse(text); // never restore a broken file
    writeFileSync(`${dir}/${f}`, text);
    console.log(`restored ${f} (${text.length} bytes)`);
  } catch (e) {
    console.log(`no previous ${f}: ${e.message}`);
  }
}
