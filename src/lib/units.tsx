import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

const KEY = "weatheredge-temp-unit";
export type TempUnit = "F" | "C";

/** Every artifact publishes Fahrenheit, and the prediction-market brackets settle
    in whole Fahrenheit degrees, so conversion happens only at display time. */
const fToC = (f: number) => ((f - 32) * 5) / 9;
/** A temperature DIFFERENCE (sigma, spread, error, gap) scales without the offset. */
const fDeltaToC = (df: number) => (df * 5) / 9;

function storedUnit(): TempUnit {
  try {
    return localStorage.getItem(KEY) === "C" ? "C" : "F";
  } catch {
    return "F";
  }
}

interface TempUnitState {
  unit: TempUnit;
  setUnit: (unit: TempUnit) => void;
}

const TempUnitContext = createContext<TempUnitState>({ unit: "F", setUnit: () => undefined });

export function TempUnitProvider({ children }: { children: ReactNode }) {
  const [unit, setUnitState] = useState<TempUnit>(() => (typeof window === "undefined" ? "F" : storedUnit()));
  useEffect(() => {
    try {
      localStorage.setItem(KEY, unit);
    } catch {
      // Storage can be disabled; the in-memory choice still applies.
    }
  }, [unit]);
  const setUnit = useCallback((next: TempUnit) => setUnitState(next), []);
  const value = useMemo(() => ({ unit, setUnit }), [unit, setUnit]);
  return <TempUnitContext.Provider value={value}>{children}</TempUnitContext.Provider>;
}

const round = (n: number, digits: number) => {
  const scale = 10 ** digits;
  return Math.round(n * scale) / scale;
};

/** Formatters for Fahrenheit inputs in the viewer's chosen unit. `temp` converts
    an absolute reading; `delta` converts a difference. Both render "—" for a
    missing value and append the unit letter only when `withUnit` is set. */
export function useTemperature() {
  const { unit, setUnit } = useContext(TempUnitContext);
  return useMemo(() => {
    const abs = (f: number) => (unit === "C" ? fToC(f) : f);
    const delta = (df: number) => (unit === "C" ? fDeltaToC(df) : df);
    const symbol = unit === "C" ? "°C" : "°F";
    const render = (n: number | null | undefined, convert: (v: number) => number, digits: number, withUnit: boolean) =>
      n == null || Number.isNaN(n) ? "—" : `${round(convert(n), digits)}${withUnit ? symbol : "°"}`;
    return {
      unit,
      setUnit,
      symbol,
      toggle: () => setUnit(unit === "C" ? "F" : "C"),
      abs,
      delta,
      temp: (f: number | null | undefined, digits = 1, withUnit = false) => render(f, abs, digits, withUnit),
      diff: (df: number | null | undefined, digits = 1, withUnit = false) => render(df, delta, digits, withUnit),
    };
  }, [unit, setUnit]);
}
