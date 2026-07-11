import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

describe("SPA release bundle gate", () => {
  it("requires a browser-observed resource list for release verification", () => {
    const pkg = JSON.parse(read("package.json"));
    expect(pkg.scripts["bundle:check:observed"]).toContain("--observed");
    expect(read("README.md")).toContain("bundle:check:observed");
    expect(read("README.md")).toContain("browser-observed");
  });
});
