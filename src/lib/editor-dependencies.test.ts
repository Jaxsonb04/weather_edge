import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Tiptap packages use exact peer contracts. Isolated updates previously left
// link/suggestion newer than core/pm; transitive caret ranges can also drift.
// Keep family upgrades reviewable without hard-coding today's patch version.
const manifest = JSON.parse(readFileSync(resolve("package.json"), "utf8")) as {
  dependencies: Record<string, string>;
  overrides: Record<string, string>;
};
const family = (entries: Record<string, string>) =>
  Object.entries(entries).filter(([name]) => name.startsWith("@tiptap/"));
const coreVersion = manifest.dependencies["@tiptap/core"];

describe("editor dependency coherence", () => {
  it("moves direct pins and transitive overrides together", () => {
    expect(coreVersion).toMatch(/^\d+\.\d+\.\d+$/);
    expect(family(manifest.dependencies).length).toBeGreaterThan(1);
    expect(family(manifest.overrides).length).toBeGreaterThan(0);
    for (const [name, version] of [
      ...family(manifest.dependencies), ...family(manifest.overrides),
    ]) {
      expect(version, name).toBe(coreVersion);
    }
  });

  it("resolves every locked editor package at the shared version", () => {
    const lock = readFileSync(resolve("bun.lock"), "utf8");
    const packages = [...lock.matchAll(
      /"[^"]+": \["(@tiptap\/[^"@]+)@([^"]+)",/g,
    )];
    const resolvedNames = new Set(packages.map((entry) => entry[1]));
    for (const [name] of [
      ...family(manifest.dependencies), ...family(manifest.overrides),
    ]) {
      expect(resolvedNames.has(name), `${name} missing from lock inspection`).toBe(true);
    }
    for (const [, name, version] of packages) {
      expect(version, name).toBe(coreVersion);
    }
  });
});
