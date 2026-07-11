import { readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { gzipSync } from "node:zlib";

const root = process.cwd();
const dist = resolve(root, "dist");
const manifest = JSON.parse(readFileSync(join(dist, ".vite/manifest.json"), "utf8"));
const landingRoots = ["index.html", "src/components/views/OverviewView.tsx"];

function gzipBytes(file) {
  return gzipSync(readFileSync(join(dist, file)), { level: 9 }).byteLength;
}

function walkManifest(keys) {
  const seen = new Set();
  const visit = (key) => {
    if (seen.has(key)) return;
    const entry = manifest[key];
    if (!entry) throw new Error(`Missing Vite manifest entry: ${key}`);
    seen.add(key);
    for (const dependency of entry.imports ?? []) visit(dependency);
  };
  keys.forEach(visit);
  return seen;
}

const landingEntries = walkManifest(landingRoots);
const landingJs = [...landingEntries].map((key) => manifest[key].file).filter((file) => file.endsWith(".js"));
const landingCss = [...new Set([...landingEntries].flatMap((key) => manifest[key].css ?? []))];
const jsGzip = landingJs.reduce((sum, file) => sum + gzipBytes(file), 0);
const cssGzip = landingCss.reduce((sum, file) => sum + gzipBytes(file), 0);
const kib = (bytes) => `${(bytes / 1024).toFixed(2)} KiB`;

console.log("Landing dependency graph (static imports only)");
for (const file of landingJs) console.log(`  JS  ${kib(gzipBytes(file)).padStart(10)}  ${file}`);
for (const file of landingCss) console.log(`  CSS ${kib(gzipBytes(file)).padStart(10)}  ${file}`);
console.log(`Landing total: JS ${kib(jsGzip)} / 300.00 KiB target; CSS ${kib(cssGzip)} / 40.00 KiB target`);
console.log(`Baseline comparison: JS ${(100 * (1 - jsGzip / (477 * 1024))).toFixed(1)}% smaller; CSS ${(100 * (1 - cssGzip / (78 * 1024))).toFixed(1)}% smaller`);

console.log("\nAll built JS/CSS chunks");
for (const directory of [join(dist, "assets")]) {
  for (const name of readdirSync(directory).sort()) {
    if (![".js", ".css"].includes(extname(name))) continue;
    const file = `assets/${name}`;
    console.log(`  ${extname(name).slice(1).toUpperCase().padEnd(3)} ${kib(statSync(join(directory, name)).size).padStart(10)} raw / ${kib(gzipBytes(file)).padStart(10)} gzip  ${file}`);
  }
}

const builtJavaScript = readdirSync(join(dist, "assets"))
  .filter((name) => name.endsWith(".js"))
  .map((name) => readFileSync(join(dist, "assets", name), "utf8"))
  .join("\n");
if (builtJavaScript.includes("api.iconify.design")) {
  throw new Error("Built JavaScript still contains the Iconify network API hostname.");
}
if (jsGzip > 300 * 1024 || cssGzip > 40 * 1024) process.exitCode = 1;
