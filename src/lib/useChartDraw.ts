import { useEffect, useState } from "react";
import { useReducedMotion } from "motion/react";

const DRAW_MS = 400;

/** Series props for a chart that draws in once. Recharts replays its animation
    on every resize and data change, so it is switched off after the first draw:
    later changes land instantly and a phone rotation never redraws the chart. */
export function useChartDraw() {
  const reduce = useReducedMotion();
  const [drawn, setDrawn] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setDrawn(true), DRAW_MS + 100);
    return () => window.clearTimeout(timer);
  }, []);
  return {
    isAnimationActive: !reduce && !drawn,
    animationDuration: DRAW_MS,
    animationEasing: "ease-out",
  } as const;
}
