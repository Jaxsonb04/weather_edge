import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { Editor, mergeAttributes } from "@tiptap/core";
import { DOMSerializer } from "@tiptap/pm/model";
import StarterKit from "@tiptap/starter-kit";

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
  it("shares one ProseMirror model version across the editor graph", () => {
    const lock = readFileSync(resolve("bun.lock"), "utf8");
    const versions = [...lock.matchAll(/"prosemirror-model@([^"]+)"/g)]
      .map((entry) => entry[1]);
    expect(versions.length).toBeGreaterThan(0);
    expect(new Set(versions).size).toBe(1);
  });

  it("keeps list and blockquote commands interoperable across extensions", () => {
    for (const [command, nodeType] of [
      ["toggleBulletList", "bulletList"],
      ["toggleOrderedList", "orderedList"],
      ["toggleBlockquote", "blockquote"],
    ] as const) {
      const editor = new Editor({
        extensions: [StarterKit], content: "<p>safe</p>",
      });
      try {
        expect(editor.commands[command](), command).toBe(true);
        expect(editor.getJSON().content?.[0]?.type, command).toBe(nodeType);
      } finally {
        editor.destroy();
      }
    }
  });

  it("does not turn imported prototype keys into inherited DOM attributes", () => {
    // GHSA-cp6q-959q-f8rh: an own JSON-origin key must not replace the
    // attribute object's prototype and reach ProseMirror's DOM serializer.
    const imported = JSON.parse('{"__proto__":{"data-inherited-canary":"present","onerror":"void 0"}}');
    const attributes = mergeAttributes({ title: "safe" }, imported);
    const prototype = Object.getPrototypeOf(attributes);
    expect(prototype === null || prototype === Object.prototype).toBe(true);
    expect(attributes["data-inherited-canary"]).toBeUndefined();
    expect(attributes.onerror).toBeUndefined();
    const { dom } = DOMSerializer.renderSpec(document, ["img", attributes]);
    expect((dom as HTMLElement).getAttribute("title")).toBe("safe");
    expect((dom as HTMLElement).hasAttribute("data-inherited-canary")).toBe(false);
    expect((dom as HTMLElement).hasAttribute("onerror")).toBe(false);
  });

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
