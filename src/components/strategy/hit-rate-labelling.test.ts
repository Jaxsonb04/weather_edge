import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Every surface that renders `paper_trading.summary.hit_rate`. The field counts
    profitable EXITS — most of them monitor take-profit or stop closes, not
    settlements — so labelling it "Hit rate" on one card and "Profitable exits"
    on the next publishes one number under two meanings. */
const READS_SUMMARY_HIT_RATE = /(summary|sum)\??\.hit_rate/;
const HONEST_LABEL = "Profitable exits";

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) return [];
    return [path];
  });
}

describe("closed-or-settled labelling", () => {
  it("labels the paper hit rate as profitable exits wherever it is rendered", () => {
    const root = resolve(process.cwd(), "src");
    const unlabelled = sourceFiles(root)
      .filter((path) => {
        const text = readFileSync(path, "utf8");
        return READS_SUMMARY_HIT_RATE.test(text) && !text.includes(HONEST_LABEL);
      })
      .map((path) => path.slice(root.length + 1));

    expect(unlabelled).toEqual([]);
  });

  it("keeps the settlement-implying labels off every surface", () => {
    const root = resolve(process.cwd(), "src");
    const offenders = sourceFiles(root)
      .filter((path) => /label[=:]\s*"(Hit rate|Resolved trades)"/.test(readFileSync(path, "utf8")))
      .map((path) => path.slice(root.length + 1));

    expect(offenders).toEqual([]);
  });
});
