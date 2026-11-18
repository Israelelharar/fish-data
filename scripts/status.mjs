// Records how every data source did this run (status.json) and manages alert issues:
//  - a source failing for more than ALERT_HOURS opens ONE issue (GitHub e-mails the repo owner),
//    with plain-Hebrew steps to fix it;
//  - when the source recovers, the issue gets a comment and is closed automatically.
// Usage: node scripts/status.mjs site/status.json   (env: OUT_<NAME>=success|failure|skipped, GH_TOKEN)
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

const out = process.argv[2] ?? "site/status.json";
const REPO = process.env.GITHUB_REPOSITORY ?? "Israelelharar/fish-data";
const ALERT_HOURS = 6;
const now = new Date();

const SOURCES = {
  gusts: { label: "משבי רוח (ICON-EU / ECMWF)", file: "gusts.json" },
  marine: { label: "גלים וטמפרטורת מים (Copernicus)", file: "marine.json" },
  accuracy: { label: "בדיקת דיוק מול תחנות השירות המטאורולוגי", file: "accuracy.json" },
  sources: { label: "מעקב אחרי מקורות רשמיים", file: "sources.json" },
};

const prev = (() => {
  try {
    return JSON.parse(readFileSync(out, "utf8")).sources ?? {};
  } catch {
    return {};
  }
})();

function errorOf(name) {
  const f = `tmp/${name}.err`;
  return existsSync(f) ? readFileSync(f, "utf8").trim().split("\n").slice(-3).join(" ").slice(0, 400) : null;
}

function advice(name, error) {
  const e = (error ?? "").toLowerCase();
  if (name === "marine" && /(invalidusernameorpassword|username|password|credential|401)/.test(e))
    return [
      "**הסיבה:** Copernicus לא מקבל את שם המשתמש או הסיסמה (כנראה הסיסמה הוחלפה באתר שלהם).",
      "**מה לעשות:**",
      "1. נכנסים ל-https://github.com/Israelelharar/fish-data/settings/secrets/actions",
      "2. לוחצים על העיפרון ליד `COPERNICUS_PASSWORD`, כותבים את הסיסמה הנוכחית של Copernicus ולוחצים **Update secret**.",
      "3. זהו. ההרצה הבאה (עד שעתיים) תתקן לבד, והתקלה הזו תיסגר אוטומטית.",
    ].join("\n");
  if (name === "marine" && /(datasetnotfound|not found|does not exist)/.test(e))
    return "**הסיבה:** Copernicus שינו שם של מאגר נתונים והחיפוש האוטומטי לא מצא חלופה. צריך לעדכן את רשימת המאגרים ב-`scripts/copernicus_marine.py` (אפשר לבקש מ-Claude).";
  return [
    "**הסיבה הסבירה:** תקלה זמנית אצל ספק הנתונים או ברשת.",
    "**מה לעשות:** בדרך כלל כלום. המערכת מנסה שוב כל שעתיים, והאפליקציה ממשיכה עם הנתונים האחרונים (עד 36 שעות) ואז עם גיבוי.",
    "אם התקלה פתוחה יותר מיומיים, אפשר להעביר אותה ל-Claude לבדיקה.",
  ].join("\n");
}

function gh(args) {
  return execFileSync("gh", args, { encoding: "utf8", env: process.env }).trim();
}

function alertIssue(name, s) {
  const title = `[תקלה] ${SOURCES[name].label}`;
  let number = "";
  try {
    number = gh(["issue", "list", "--repo", REPO, "--state", "open", "--search", `in:title "${title}"`, "--json", "number", "--jq", ".[0].number // empty"]);
  } catch (e) {
    console.log(`cannot list issues: ${e.message}`);
    return;
  }
  if (!s.ok && !number) {
    const body = [
      `המקור **${SOURCES[name].label}** לא מתעדכן כבר ${s.hoursSinceSuccess ?? "?"} שעות (${s.failures} ניסיונות כושלים ברצף).`,
      "",
      advice(name, s.error),
      "",
      "```",
      s.error ?? "(no error text)",
      "```",
    ].join("\n");
    gh(["issue", "create", "--repo", REPO, "--title", title, "--body", body]);
    console.log(`opened alert: ${title}`);
  } else if (s.ok && number) {
    gh(["issue", "close", number, "--repo", REPO, "--comment", "חזר לעבוד ✓ (נסגר אוטומטית)"]);
    console.log(`closed alert #${number}`);
  }
}

const sources = {};
for (const name of Object.keys(SOURCES)) {
  const outcome = process.env[`OUT_${name.toUpperCase()}`] ?? "skipped";
  const p = prev[name] ?? {};
  let s;
  if (outcome === "success") s = { ok: true, lastSuccess: now.toISOString(), failures: 0, error: null };
  else if (outcome === "failure") s = { ok: false, lastSuccess: p.lastSuccess ?? null, failures: (p.failures ?? 0) + 1, error: errorOf(name) ?? p.error ?? null };
  else s = { ok: null, lastSuccess: p.lastSuccess ?? null, failures: 0, error: null, off: true };
  s.hoursSinceSuccess = s.lastSuccess ? Math.round((now - new Date(s.lastSuccess)) / 36e5) : null;
  sources[name] = s;
  console.log(`${name}: ${outcome} (last success ${s.lastSuccess ?? "never"})`);

  const overdue = s.ok === false && (s.hoursSinceSuccess == null ? s.failures >= 3 : s.hoursSinceSuccess >= ALERT_HOURS);
  if (process.env.GH_TOKEN && (overdue || s.ok === true)) alertIssue(name, s);
}

writeFileSync(out, JSON.stringify({ updated: now.toISOString(), sources }, null, 1));
