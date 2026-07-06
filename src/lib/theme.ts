import { useCallback, useEffect, useState } from "react";

const KEY = "weatheredge-theme";
export type ThemeMode = "dark" | "light";

function applyMode(mode: ThemeMode) {
  document.documentElement.classList.toggle("dark", mode === "dark");
}

/** Pins the instrument theme to an explicit .dark class on <html>, persisted.
    Demonstrates HeroUI v3 theming: tokens auto-resolve per mode, no provider. */
export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(() => {
    if (typeof window === "undefined") return "dark";
    return (localStorage.getItem(KEY) as ThemeMode | null) ?? "dark";
  });

  useEffect(() => {
    applyMode(mode);
    localStorage.setItem(KEY, mode);
  }, [mode]);

  const toggle = useCallback(
    () => setMode((m) => (m === "dark" ? "light" : "dark")),
    [],
  );

  return { mode, toggle };
}
