import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

describe("named motion fallbacks", () => {
  it.each([
    "src/App.tsx",
    "src/components/States.tsx",
    "src/components/views/MethodologyView.tsx",
    "src/components/views/StrategyLabView.tsx",
  ])("disables named spinner/ping animation in %s", (path) => {
    const source = read(path);
    expect(source.match(/animate-(?:spin|ping)[^"]*/g)?.every((classes) => classes.includes("motion-reduce:animate-none"))).toBe(true);
  });

  it("disables named width and hover-transform transitions", () => {
    expect(read("src/components/market/DecisionCard.tsx")).toContain("motion-reduce:transition-none");
    const city = read("src/components/overview/CityGrid.tsx");
    expect(city).toContain("motion-reduce:transition-none");
    expect(city).toContain("motion-reduce:hover:translate-y-0");
  });
});
