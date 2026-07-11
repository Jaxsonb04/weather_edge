/// <reference types="node" />

import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useTheme } from "./theme";

function Probe() {
  const { mode } = useTheme();
  return <p>{mode}</p>;
}

describe("theme startup", () => {
  afterEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove("dark");
    vi.restoreAllMocks();
  });

  it("honors the operating-system theme when no preference is stored", () => {
    localStorage.clear();
    render(<Probe />);

    expect(screen.getByText("light")).toBeInTheDocument();
    expect(document.documentElement).not.toHaveClass("dark");
  });

  it("keeps a stored system preference and resolves its current dark mode", () => {
    localStorage.setItem("weatheredge-theme", "system");
    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    } as unknown as MediaQueryList);
    render(<Probe />);

    expect(screen.getByText("dark")).toBeInTheDocument();
    expect(localStorage.getItem("weatheredge-theme")).toBe("system");
  });

  it("runs the guarded theme decision before the application module", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const themeScript = html.indexOf("weatheredge-theme");
    const application = html.indexOf('src="/src/main.tsx"');

    expect(themeScript).toBeGreaterThan(0);
    expect(themeScript).toBeLessThan(application);
    expect(html).toContain("prefers-color-scheme: dark");
    expect(html).toContain("try {");
  });
});
