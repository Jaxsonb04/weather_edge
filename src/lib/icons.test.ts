/// <reference types="node" />

import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { weatherEdgeIcons } from "../generated/icon-collection";

function sources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return entry.name === "generated" ? [] : sources(path);
    return /\.[jt]sx?$/.test(entry.name) ? [readFileSync(path, "utf8")] : [];
  });
}

describe("offline Iconify collection", () => {
  it("contains exactly the Solar glyphs referenced by source", () => {
    const source = sources(resolve(process.cwd(), "src")).join("\n");
    const names = [...source.matchAll(/\bsolar:([a-z0-9-]+)\b/g)].map((match) => match[1]);

    expect(Object.keys(weatherEdgeIcons.icons).sort()).toEqual([...new Set(names)].sort());
    expect(source).not.toMatch(/\bmdi:[a-z0-9-]+\b/);
    expect(Object.keys(weatherEdgeIcons.icons).length).toBeLessThan(100);
  });

  it("regenerates deterministically from official installed icon data", () => {
    expect(() => execFileSync(process.execPath, ["scripts/generate_icon_collection.mjs", "--check"], {
      cwd: process.cwd(),
      stdio: "pipe",
    })).not.toThrow();
  });
});
