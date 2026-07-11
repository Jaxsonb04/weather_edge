import { useCallback, useEffect, useState } from "react";

const KEY = "weatheredge-theme";
export type ThemeMode = "dark" | "light";
type ThemePreference = ThemeMode | "system";

function applyMode(mode: ThemeMode) {
  document.documentElement.classList.toggle("dark", mode === "dark");
  document.documentElement.style.colorScheme = mode;
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
