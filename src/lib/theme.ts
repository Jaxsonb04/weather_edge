import { useCallback, useEffect, useState } from "react";

const KEY = "weatheredge-theme";
export type ThemeMode = "dark" | "light";
type ThemePreference = ThemeMode | "system";

/** Must stay in step with the two media-scoped tags in index.html. */
const THEME_COLOR = { dark: "#14151a", light: "#f5f5f5" } as const;

function applyMode(mode: ThemeMode) {
  document.documentElement.classList.toggle("dark", mode === "dark");
  document.documentElement.style.colorScheme = mode;
  // index.html ships one media-scoped theme-color per scheme, which resolves
  // the address bar correctly on load. A mid-session toggle contradicts the OS
  // preference those queries key on, so collapse them to a single explicit tag.
  const tags = document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]');
  tags.forEach((tag, index) => {
    if (index > 0) {
      tag.remove();
      return;
    }
    tag.removeAttribute("media");
    tag.content = THEME_COLOR[mode];
  });
}

function storedPreference(): ThemePreference {
  try {
    const value = localStorage.getItem(KEY);
    return value === "dark" || value === "light" || value === "system" ? value : "system";
  } catch {
    return "system";
  }
}

function systemMode(): ThemeMode {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Resolves a persisted light/dark/system preference into the explicit class
    HeroUI consumes. The matching head script does the same before first paint. */
export function useTheme() {
  const [preference, setPreference] = useState<ThemePreference>(() => {
    if (typeof window === "undefined") return "system";
    return storedPreference();
  });
  const [mode, setMode] = useState<ThemeMode>(() => {
    if (typeof window === "undefined") return "dark";
    const preference = storedPreference();
    return preference === "system" ? systemMode() : preference;
  });

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => {
      const resolved = preference === "system" ? (media.matches ? "dark" : "light") : preference;
      setMode(resolved);
      applyMode(resolved);
    };
    sync();
    try {
      localStorage.setItem(KEY, preference);
    } catch {
      // Storage can be disabled; the applied in-memory preference still works.
    }
    if (preference !== "system") return;
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, [preference]);

  const toggle = useCallback(
    () => setPreference(mode === "dark" ? "light" : "dark"),
    [mode],
  );

  return { mode, toggle };
}
