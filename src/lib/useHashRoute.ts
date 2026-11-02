import { useEffect, useState } from "react";

export type Route = "overview" | "methodology" | "lab";
export const ROUTES: { id: Route; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "solar:widget-5-linear" },
  { id: "methodology", label: "Methodology", icon: "solar:graph-up-linear" },
  { id: "lab", label: "Strategy Lab", icon: "solar:test-tube-linear" },
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

function subFrom<T extends string>(options: readonly T[], fallback: T): T {
  const { sub } = parse();
  return options.includes(sub as T) ? (sub as T) : fallback;
}

/** One sub-page of `route`, kept in step with `#/<route>/<sub>`. Links, the
    command palette and edited URLs change the hash while the view stays
    mounted, so the selection follows them. `select` rewrites the hash with
    replaceState, which fires no hashchange (that would scroll the page to the
    top on every switch). A missing or unknown segment reads as `fallback`. */
export function useHashSub<T extends string>(route: Route, options: readonly T[], fallback: T) {
  const [value, setValue] = useState<T>(() => subFrom(options, fallback));
  useEffect(() => {
    const follow = () => {
      // Leaving the route is not a sub-page change.
      if (parse().route === route) setValue(subFrom(options, fallback));
    };
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, [route, options, fallback]);
  const select = (next: T) => {
    setValue(next);
    try {
      window.history.replaceState(null, "", `#/${route}/${next}`);
    } catch {
      // Sandboxed frames can refuse history writes; the selection still changes.
    }
  };
  return [value, select] as const;
}
