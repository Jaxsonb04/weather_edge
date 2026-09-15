import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level guard for public copy that states the city count (release
// review 2026-09-13): the registry grew from fifteen to twenty cities while
// the README coverage row, the social card and its alt text kept saying
// fifteen. The count is read from the registry itself (forecaster/cities.py,
// kept byte-identical to trading/sfo_kalshi_quant/cities.py by a Python test),
// so the next registry change fails here until the copy follows it.

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

const NUMBER_WORDS = [
  "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
  "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
  "Seventeen", "Eighteen", "Nineteen", "Twenty", "Twenty-one", "Twenty-two",
  "Twenty-three", "Twenty-four", "Twenty-five",
];

const registryCityCount = () => {
  const source = read("forecaster/cities.py");
  const slugs = new Set<string>();
  const pattern = /\bslug="([a-z]+)"/g;
  for (let match = pattern.exec(source); match !== null; match = pattern.exec(source)) {
    slugs.add(match[1]);
  }
  return slugs.size;
};

const metaContent = (html: string, attribute: "name" | "property", key: string) => {
  const tag = html.match(new RegExp(`<meta\\s+${attribute}="${key}"\\s+content="([^"]*)"`));
  expect(tag, `${attribute}="${key}" meta tag`).not.toBeNull();
  return tag?.[1] ?? "";
};

describe("public city-count copy", () => {
  const count = registryCityCount();
  const word = NUMBER_WORDS[count];

  it("reads a city count it can spell", () => {
    expect(count).toBeGreaterThan(1);
    expect(word).toBeDefined();
  });

  it("states the registry count in the page, og and twitter descriptions", () => {
    const html = read("index.html");
    expect(metaContent(html, "name", "description")).toContain(`across ${count} U.S. cities`);
    expect(metaContent(html, "property", "og:description")).toContain(
      `across ${count} U.S. city markets`,
    );
    expect(metaContent(html, "name", "twitter:description")).toContain(
      `across ${count} U.S. city markets`,
    );
  });

  it("describes the social card with the count the card renders", () => {
    const html = read("index.html");
    const headline = `${word}-city temperature forecasting`;
    expect(metaContent(html, "property", "og:image:alt")).toContain(headline);
    expect(metaContent(html, "name", "twitter:image:alt")).toContain(headline);
    expect(read("docs/assets/og-weatheredge-v2.html")).toContain(
      `<h1>${word}-city <span>temperature forecasting</span></h1>`,
    );
  });

  it("states the registry count in the README coverage row", () => {
    expect(read("README.md")).toContain(`| Coverage | ${count} city markets,`);
  });

  it("leaves no stale fifteen-city count in that copy", () => {
    const copy = [read("index.html"), read("README.md"), read("docs/assets/og-weatheredge-v2.html")];
    for (const text of copy) {
      expect(text).not.toMatch(/Fifteen-city|\b15 (U\.S\. )?city markets|\b15 U\.S\. cities/);
    }
  });
});
