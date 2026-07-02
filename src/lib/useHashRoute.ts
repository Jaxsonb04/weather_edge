import { useEffect, useState } from "react";

export type Route = "overview" | "methodology" | "lab";
export const ROUTES: { id: Route; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "solar:widget-5-bold" },
  { id: "methodology", label: "Methodology", icon: "solar:graph-up-bold" },
  { id: "lab", label: "Strategy Lab", icon: "solar:test-tube-bold" },
];

function parse(): { route: Route; sub: string | null } {
  const segs = window.location.hash.replace(/^#\/?/, "").split(/[?#]/)[0].split("/");
  const route = (ROUTES.some((r) => r.id === segs[0]) ? segs[0] : "overview") as Route;
  return { route, sub: segs[1] || null };
}

/** Tiny hash router — gh-pages-safe (no server rewrites), no router dependency.
    Supports one optional sub-segment (e.g. #/lab/trades → route "lab", sub "trades"). */
export function useHashRoute() {
  const [state, setState] = useState(parse);
  useEffect(() => {
    const onHash = () => {
      setState(parse());
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const navigate = (r: Route) => {
    if (parse().route === r) window.scrollTo({ top: 0 }); // re-selecting current route still scrolls up
    window.location.hash = `#/${r}`;
  };
  return { route: state.route, sub: state.sub, navigate };
}
