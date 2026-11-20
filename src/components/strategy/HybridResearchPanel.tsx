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
const linkClass = "inline-flex min-h-11 items-center text-sm font-medium text-[color:var(--accent-text)] underline-offset-4 hover:underline focus-visible:rounded focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)]";
const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0
  ? new Intl.NumberFormat("en-US").format(value) : "Unpublished";

/** Runtime identity comes from the ledger feed; offline experiments have their
 * own dated artifact and never change readiness or the active policy label. */
export function HybridResearchPanel({ s }: { s: StrategyLab }) {
  const { data: snapshot } = useResource<ResearchSnapshot>("hybrid_research.json");
  const target = activeProfiles(s).find((profile) => profile.risk_profile === "research-target");
  const policy = researchDailyTarget(s, target)?.policy_version;
  const runningCurrent = typeof policy === "string" && /^research-target-(?:roi-v7|hf-v8)(?:$|[-.])/.test(policy);
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
    <section aria-labelledby="hybrid-research-title" className="flex min-w-0 flex-col gap-4 rounded-3xl bg-surface-secondary p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id="hybrid-research-title" className="text-sm font-semibold text-foreground">Research experiments</h3>
          <p className="mt-0.5 text-xs text-muted">Offline experiments, kept apart from the running accounts</p>
        </div>
        <span className="rounded-full bg-warning-soft px-3 py-1 text-xs font-medium text-foreground">Paper only · live allocation $0</span>
      </div>
      <p className="text-sm leading-relaxed text-muted">
        {runningCurrent
          ? `The published research ledger reports the current policy, ${policy}. Its new evidence remains separate from earlier generations.`
          : policy
            ? `The published research ledger still reports ${policy}. Current-policy source and offline experiments do not establish a production track record for it.`
            : "The research ledger has not published a policy identity. Current-policy activation is unverified."}
        {" "}Real-money execution {s.live_orders_enabled === false ? "is disabled in this publication" : s.live_orders_enabled === true ? "is unexpectedly reported enabled; the paper-only policy requires investigation" : "has no published flag in this artifact"}.
      </p>
      {hasCollection && <p className="text-sm leading-relaxed text-muted">
        <strong className="text-foreground">Dated Mac batch · {collectionDate} Pacific.</strong>{" "}
        {count(collection?.new_issued_forecasts)} newly issued forecasts from {count(collection?.http_requests)} public weather requests in {collection?.elapsed_seconds?.toFixed(1)} seconds.
        {" "}These are collected forecast versions, not independent completed weather days or profitable fills.
      </p>}
      <div className="flex flex-col gap-2">
        <DetailDisclosure id="hybrid-model-evidence" icon="solar:chart-square-linear" title="Local model comparison" note={date ? `Dated research snapshot · ${date} · never a profit estimate` : "Research evidence has not loaded; no model improvement is claimed"}>
          <p className="text-sm leading-relaxed text-muted">Gaussian calibration and shallow boosted temperature quantiles, run against the existing forecast and bias correction. Reconstructed history is exploratory; only verified decision-time vintages can qualify for prospective validation, and weather error improvements alone do not prove profitable fills.</p>
          {snapshot?.evaluation_kind === "retrospective_diagnostic" && (
            <>
              <dl className="grid gap-3 sm:grid-cols-3">
                {[["Paired cases", snapshot.cases], ["Distinct target dates", snapshot.distinct_target_dates], ["Qualified original vintages", snapshot.qualified_original_vintage_cases]].map(([label, value]) => (
                  <div key={String(label)}><dt className="text-xs text-muted">{label}</dt><dd className="tnum mt-1 text-lg font-semibold text-foreground">{count(value)}</dd></div>
                ))}
              </dl>
              {!!candidates.length && <ul className="flex flex-col gap-3">{candidates.map((candidate, index) => (
                <li key={index} className="text-sm leading-relaxed text-muted"><strong className="text-foreground">{candidate.name ?? "Unnamed candidate"}</strong>{candidate.comparison ? ` · ${candidate.comparison}` : ""}<span className="mt-1 block text-xs">{candidate.state ?? "No promotion decision published"}</span></li>
              ))}</ul>}
            </>
          )}
          <a className={linkClass} href={`${REPO_DOCS}research/2026-10-05-hybrid-ml.md`}>Read methods, results, and research sources</a>
        </DetailDisclosure>
        <DetailDisclosure id="hybrid-compute" icon="solar:server-square-cloud-linear" title="How the research is run" note="AWS collects, the Mac experiments, evidence decides">
          <dl className="grid min-w-0 gap-3 md:grid-cols-3">
            {[
              ["AWS · continuous collection", "Weather, market quotes, paper execution, settlement truth and public data. Collection continues when the Mac is offline."],
              ["Mac · bounded research", "A separate prospective weather shadow from free research APIs, audits and chronological model comparisons, under strict resource limits."],
              ["Promotion · evidence first", "After-fee filled outcomes, original forecast vintages, account reconciliation and the readiness gates. No local job promotes a model or money."],
            ].map(([title, text]) => (
              <div key={title} className="min-w-0 rounded-2xl bg-surface-secondary p-3">
                <dt className="text-sm font-medium text-foreground">{title}</dt>
                <dd className="mt-1 text-xs leading-relaxed text-muted">{text}</dd>
              </div>
            ))}
          </dl>
          <p className="text-sm leading-relaxed text-muted">
            The readiness policy needs 30 independent weather dates after a qualifying behavior change. Initial capital stays undetermined until a positive conservative net edge and an explicit loss budget support it.
          </p>
          <a className={linkClass} href={`${REPO_DOCS}local_compute.md`}>Inspect the compute workflow and limits</a>
        </DetailDisclosure>
      </div>
    </section>
  );
}
