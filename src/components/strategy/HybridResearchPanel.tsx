import { useResource } from "../../lib/data";
import { activeProfiles, researchDailyTarget, type StrategyLab } from "../../lib/strategy";
import { DetailDisclosure } from "../ui/DetailDisclosure";

interface ResearchSnapshot {
  captured_at?: string;
  evaluation_kind?: string;
  cases?: number;
  distinct_target_dates?: number;
  qualified_original_vintage_cases?: number;
  candidates?: Array<{ name?: string; state?: string; comparison?: string }>;
  local_collection?: {
    finished_at?: string;
    new_issued_forecasts?: number;
    http_requests?: number;
    elapsed_seconds?: number;
  };
}

const REPO_DOCS = "https://github.com/Jaxsonb04/weather_edge/blob/main/docs/";
const linkClass = "inline-flex min-h-11 items-center text-sm font-medium text-[color:var(--accent-text)] underline underline-offset-4 hover:text-foreground focus-visible:rounded focus-visible:ring-2 focus-visible:ring-[color:var(--focus)]";
const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0
  ? new Intl.NumberFormat("en-US").format(value) : "Unpublished";

/** Runtime identity comes from the ledger feed; offline experiments have their
 * own dated artifact and never change readiness or the active policy label. */
export function HybridResearchPanel({ s }: { s: StrategyLab }) {
  const { data: snapshot } = useResource<ResearchSnapshot>("hybrid_research.json");
  const target = activeProfiles(s).find((profile) => profile.risk_profile === "research-target");
  const policy = researchDailyTarget(s, target)?.policy_version;
  const runningV7 = typeof policy === "string" && /^research-target-roi-v7(?:$|[-.])/.test(policy);
  const stamp = snapshot?.captured_at ? new Date(snapshot.captured_at) : null;
  const date = stamp && Number.isFinite(stamp.getTime())
    ? new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeZone: "America/Los_Angeles" }).format(stamp)
    : null;
  const collection = snapshot?.local_collection;
  const collectionClock = typeof collection?.finished_at === "string" ? new Date(collection.finished_at) : null;
  const collectionDate = collectionClock && Number.isFinite(collectionClock.getTime())
    ? new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short", timeZone: "America/Los_Angeles" }).format(collectionClock)
    : null;
  const hasCollection = collectionDate && typeof collection?.new_issued_forecasts === "number"
    && Number.isSafeInteger(collection.new_issued_forecasts) && collection.new_issued_forecasts >= 0
    && typeof collection.http_requests === "number" && Number.isSafeInteger(collection.http_requests) && collection.http_requests >= 0
    && typeof collection.elapsed_seconds === "number" && Number.isFinite(collection.elapsed_seconds) && collection.elapsed_seconds >= 0;
  const candidates = Array.isArray(snapshot?.candidates)
    ? snapshot.candidates.filter((candidate) => candidate && typeof candidate === "object" && !Array.isArray(candidate)).map((candidate) => ({
      name: typeof candidate.name === "string" ? candidate.name : "Unnamed candidate",
      comparison: typeof candidate.comparison === "string" ? candidate.comparison : undefined,
      state: typeof candidate.state === "string" ? candidate.state : "No promotion decision published",
    })) : [];
  return (
    <section aria-labelledby="hybrid-research-title" className="mt-6 min-w-0 rounded-2xl border border-border/70 bg-surface/80 p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-wide text-muted">V7 research workbench</p>
          <h3 id="hybrid-research-title" className="mt-1 font-display text-lg font-semibold text-foreground">Compute follows the evidence</h3>
        </div>
        <span className="rounded-full bg-warning/10 px-3 py-1.5 text-xs font-medium text-warning">Paper only · live allocation $0</span>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-muted">
        {runningV7
          ? "The published research ledger reports V7. Its new evidence remains separate from earlier generations."
          : policy
            ? `The published research ledger still reports ${policy}. V7 source and offline experiments do not establish a V7 production track record.`
            : "The research ledger has not published a policy identity. V7 activation is unverified."}
        {" "}Real-money execution {s.live_orders_enabled === false ? "is disabled in this publication" : s.live_orders_enabled === true ? "is unexpectedly reported enabled; the paper-only policy requires investigation" : "has no published flag in this artifact"}.
      </p>
      <dl className="mt-5 grid min-w-0 gap-3 md:grid-cols-3">
        {[
          ["AWS · continuous collection", "Weather collection, market quotes, paper execution, final settlement truth, and public data. Collection continues when the Mac is unavailable; original V7 vintage capture still requires guarded backend activation."],
          ["Mac · bounded research", "A separate prospective weather shadow from free research APIs, plus audits and chronological model comparisons. One compute thread, AC power, low priority, and automatic resource stops."],
          ["Promotion · evidence first", "After-fee filled outcomes, original forecast vintages, account reconciliation, and the existing readiness gates. No model or money is promoted by a local job."],
        ].map(([title, text]) => (
          <div key={title} className="min-w-0 rounded-xl bg-surface-secondary/60 p-3 ring-1 ring-border/50">
            <dt className="text-sm font-semibold text-foreground">{title}</dt>
            <dd className="mt-2 text-xs leading-relaxed text-muted">{text}</dd>
          </div>
        ))}
      </dl>
      {hasCollection && <p className="mt-4 text-sm leading-relaxed text-muted">
        <strong className="text-foreground">Dated Mac batch · {collectionDate} Pacific.</strong>{" "}
        {count(collection?.new_issued_forecasts)} newly issued forecasts from {count(collection?.http_requests)} public weather requests; the complete research run took {collection?.elapsed_seconds?.toFixed(1)} seconds.
        {" "}These are collected forecast versions, not independent completed weather days or profitable fills. Later Mac runs remain private until another research snapshot is published.
      </p>}
      <p className="mt-4 text-sm leading-relaxed text-muted">
        October 31 is an engineering and evidence review date. The current readiness policy requires 30 independent weather dates after a qualifying behavior change; an unactivated V7 cannot collect that record by the review date. Initial capital remains undetermined until a positive conservative net edge and an explicit loss budget support it.
      </p>
      <div className="mt-4">
        <DetailDisclosure id="hybrid-model-evidence" icon="solar:chart-square-bold" title="Local model comparison" note={date ? `Dated research snapshot · ${date} · never a profit estimate` : "Research evidence has not loaded; no model improvement is claimed"}>
          <p className="text-sm leading-relaxed text-muted">Gaussian calibration and shallow boosted temperature quantiles run against the existing forecast and bias correction. Reconstructed history is an exploratory diagnostic. The Mac shadow has its own identity and uses providers approved for noncommercial research; it does not extend AWS’s trading record. Only verified decision-time vintages can qualify for prospective validation; weather error improvements alone do not prove profitable fills.</p>
          {snapshot?.evaluation_kind === "retrospective_diagnostic" && (
            <>
              <dl className="grid gap-3 sm:grid-cols-3">
                {[["Paired cases", snapshot.cases], ["Distinct target dates", snapshot.distinct_target_dates], ["Qualified original vintages", snapshot.qualified_original_vintage_cases]].map(([label, value]) => (
                  <div key={String(label)}><dt className="text-xs text-muted">{label}</dt><dd className="tnum mt-1 text-lg font-semibold text-foreground">{count(value)}</dd></div>
                ))}
              </dl>
              {!!candidates.length && <ul className="space-y-3">{candidates.map((candidate, index) => (
                <li key={index} className="text-sm leading-relaxed text-muted"><strong className="text-foreground">{candidate.name ?? "Unnamed candidate"}</strong>{candidate.comparison ? ` · ${candidate.comparison}` : ""}<span className="mt-1 block text-xs">{candidate.state ?? "No promotion decision published"}</span></li>
              ))}</ul>}
            </>
          )}
          <a className={linkClass} href={`${REPO_DOCS}research/2026-10-05-hybrid-ml.md`}>Read methods, results, and research sources</a>
        </DetailDisclosure>
      </div>
      <a className={`${linkClass} mt-3`} href={`${REPO_DOCS}local_compute.md`}>Inspect the compute workflow and limits</a>
    </section>
  );
}
