import { writeFile } from "node:fs/promises";
import { chromium } from "playwright-core";

const output = process.argv[2];
if (!output) throw new Error("usage: node scripts/capture_initial_resources.mjs OUTPUT_PATH");

const baseUrl = process.env.WEATHEREDGE_PREVIEW_URL ?? "http://127.0.0.1:4173/";
const executablePath = process.env.CHROME_PATH || undefined;
const browser = await chromium.launch({ headless: true, executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const resources = new Set();
page.on("response", (response) => {
  const url = new URL(response.url());
  if (/\/assets\/[A-Za-z0-9_.-]+\.(?:js|css)$/.test(url.pathname)) {
    resources.add(url.pathname.replace(/^\//, ""));
  }
});
await page.goto(baseUrl, { waitUntil: "networkidle" });
await writeFile(output, `${[...resources].sort().join("\n")}\n`, "utf8");
await browser.close();
if (!resources.size) throw new Error(`no initial JS/CSS resources observed at ${baseUrl}`);
