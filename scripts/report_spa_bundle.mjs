import { readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { gzipSync } from "node:zlib";

const root = process.cwd();
const dist = resolve(root, "dist");
const manifest = JSON.parse(readFileSync(join(dist, ".vite/manifest.json"), "utf8"));
const landingRoots = ["index.html", "src/components/views/OverviewView.tsx"];
const observedFlag = process.argv.indexOf("--observed");
const observedPath = observedFlag >= 0 ? process.argv[observedFlag + 1] : null;
if (observedFlag >= 0 && !observedPath) throw new Error("--observed requires a browser resource-list path");

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

console.log("Manifest-declared landing graph (static imports only; not a browser observation)");
for (const file of landingJs) console.log(`  JS  ${kib(gzipBytes(file)).padStart(10)}  ${file}`);
for (const file of landingCss) console.log(`  CSS ${kib(gzipBytes(file)).padStart(10)}  ${file}`);
console.log(`Manifest static total: JS ${kib(jsGzip)}; CSS ${kib(cssGzip)}`);

let budgetJs = jsGzip;
let budgetCss = cssGzip;
let budgetLabel = "Manifest static graph (browser observation not supplied)";

if (observedPath) {
  const source = readFileSync(resolve(root, observedPath), "utf8");
  const matches = source.match(/assets\/[A-Za-z0-9_.-]+\.(?:js|css)/g) ?? [];
  const requestedFiles = [...new Set(matches)];
  const missingFiles = requestedFiles.filter((file) => {
    try {
      return !statSync(join(dist, file)).isFile();
    } catch {
      return true;
    }
  });
  if (!requestedFiles.length) throw new Error(`No built JS/CSS resources found in observed list: ${observedPath}`);
  if (missingFiles.length) {
    throw new Error(`Observed list does not match the current build: ${missingFiles.join(", ")}`);
  }
  const observedFiles = requestedFiles;
  const observedJs = observedFiles.filter((file) => file.endsWith(".js"));
  const observedCss = observedFiles.filter((file) => file.endsWith(".css"));
  budgetJs = observedJs.reduce((sum, file) => sum + gzipBytes(file), 0);
  budgetCss = observedCss.reduce((sum, file) => sum + gzipBytes(file), 0);
  budgetLabel = "Browser-observed initial graph";
  console.log(`\n${budgetLabel} (${observedPath})`);
  for (const file of observedJs) console.log(`  JS  ${kib(gzipBytes(file)).padStart(10)}  ${file}`);
  for (const file of observedCss) console.log(`  CSS ${kib(gzipBytes(file)).padStart(10)}  ${file}`);
}

console.log(`${budgetLabel}: JS ${kib(budgetJs)} / 300.00 KiB target; CSS ${kib(budgetCss)} / 40.00 KiB target`);
console.log(`Baseline comparison: JS ${(100 * (1 - budgetJs / (477 * 1024))).toFixed(1)}% smaller; CSS ${(100 * (1 - budgetCss / (78 * 1024))).toFixed(1)}% smaller`);

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
if (budgetJs > 300 * 1024 || budgetCss > 40 * 1024) process.exitCode = 1;
