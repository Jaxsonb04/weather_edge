import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

/** Route headings (and the skip-link target) receive focus from script on
    navigation so screen readers land on the new page. They are not
    interactive, so that programmatic focus must not paint a ring; real
    keyboard focus rings stay on the interactive controls. */
describe("programmatic focus targets", () => {
  it.each([
    "src/components/hero/Hero.tsx",
    "src/components/ui/PageHeader.tsx",
    "src/components/views/OverviewView.tsx",
  ])("keeps the route heading focusable but ring-free in %s", (path) => {
    const source = read(path);
    // From the element's opening through its className attribute.
    const heading = source.match(/<h1\s+id=[\s\S]*?className="[^"]*"/)?.[0] ?? "";
    expect(heading).toContain("tabIndex={-1}");
    expect(heading).toContain("focus:outline-none");
    expect(heading).not.toMatch(/focus-visible:outline/);
  });

  it("keeps the skip-link target ring-free", () => {
    const main = read("src/App.tsx").match(/<main\s+ref=[\s\S]*?className="[^"]*"/)?.[0] ?? "";
    expect(main).toContain("tabIndex={-1}");
    expect(main).toContain("focus:outline-none");
  });
});
