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
    // Spinners were replaced by skeletons in the redesign; any that remain (or
    // return) must still carry a reduced-motion fallback.
    const named = source.match(/animate-(?:spin|ping)[^"]*/g) ?? [];
    expect(named.every((classes) => classes.includes("motion-reduce:animate-none"))).toBe(true);
  });

  it("disables named width and hover-transform transitions", () => {
    expect(read("src/components/market/DecisionCard.tsx")).toContain("motion-reduce:transition-none");
    const city = read("src/components/overview/CityGrid.tsx");
    expect(city).toContain("motion-reduce:transition-none");
    expect(city).toContain("motion-reduce:hover:translate-y-0");
  });

  it("stops the loading shimmer sweep under reduced motion", () => {
    // HeroUI animates the shimmer on pseudo-elements and never stops it itself.
    const css = read("src/index.css");
    const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced).toMatch(/\.skeleton--shimmer::before,\s*\.skeleton--shimmer::after\s*\{\s*animation: none;/);
  });
});
