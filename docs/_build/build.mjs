// Builds every doc listed in manifest.json: docs/src/<slug>.md -> docs/pdf/<code>-<slug>.pdf
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const manifest = JSON.parse(readFileSync(resolve(HERE, "manifest.json"), "utf8"));

const only = process.argv[2]; // optional slug filter
let ok = 0, miss = 0, fail = 0;
for (const d of manifest.docs) {
  if (only && d.slug !== only) continue;
  const src = resolve(ROOT, "src", d.slug + ".md");
  const out = resolve(ROOT, "pdf", `${d.code}-${d.slug}.pdf`);
  if (!existsSync(src)) { console.log(`MISS  ${d.slug} (no src/${d.slug}.md)`); miss++; continue; }
  try {
    execFileSync("node", [resolve(HERE, "render.mjs"), src, out, JSON.stringify(d)], { stdio: "pipe" });
    console.log(`OK    ${d.code}  ${d.title}`);
    ok++;
  } catch (e) {
    console.log(`FAIL  ${d.slug}: ${String(e.stderr || e).split("\n").slice(-3).join(" ")}`);
    fail++;
  }
}
console.log(`\n${ok} built, ${miss} missing source, ${fail} failed (of ${manifest.docs.length} in manifest)`);
if (fail) process.exit(1);
