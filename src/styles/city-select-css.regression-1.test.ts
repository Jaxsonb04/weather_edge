import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Regression: ISSUE-001 — the above-fold city menu rendered before its item CSS.
// Report: .gstack/qa-reports/qa-report-jaxsonb04-github-io-2026-07-20.md
describe("above-fold city selector styles", () => {
  it("ships list-box item sizing in the eager stylesheet", () => {
    const styles = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");

    expect(styles).toContain('@import "@heroui/styles/components/list-box-item.css"');
  });
});
