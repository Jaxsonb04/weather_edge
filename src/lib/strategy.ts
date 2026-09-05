import { cityForTicker, useResource } from "./data";

/** Sections the bounded public refresh publishes as an unpopulated stub rather
    than a measurement. `fast_public` is the permanent public mode, so a deferred
    section is a standing state, not an outage: its zeros must never be rendered
    as a measured result. Where the publisher stamps the stub it carries
    `available: false` plus a `reason`; older stubs carry neither, so each
    consumer also detects the empty shape. */
export interface DeferrableSection {
  available?: boolean;
  reason?: string;
}

/** Used when a deferred section publishes no reason of its own. */
const DEFERRED_WITHOUT_REASON =
  "Not published in this bounded public refresh, which defers the full decision-journal analytics behind it.";

/** The reason a section gives for being deferred, or a neutral stand-in. */
export function deferralReason(section: DeferrableSection | undefined): string {
  const published = section?.reason?.trim();
  return published ? published : DEFERRED_WITHOUT_REASON;
}

/** Outcome vocabulary emitted by the paper runtime (`paper_card.py` writes
    good/bad/warn). The HeroUI colour names are kept as accepted aliases so an
    older artifact still renders, and the union makes a future vocabulary change
    fail type-check instead of degrading to a grey chip. */
export type PositionStatusTone = "good" | "bad" | "warn" | "success" | "danger" | "warning";

export interface ClosedPosition {
  id: number;
  ticker: string;
  label: string;
  side: string;
  contracts: number;
  entry_price: number;
  exit_price: number | null;
  realized_pnl: number;
  realized_roi: number | null;
  quality_score: number;
  risk_profile: string;
  target_date: string;
  /** Null for every position held to settlement — those carry `settled_at`. */
  closed_at: string | null;
  settled_at?: string | null;
  filled_at?: string | null;
  cancelled_at?: string | null;
  expires_at?: string | null;
  account_id?: string | null;
  strategy_fingerprint?: string | null;
  sleeve?: string | null;
  fill_model?: string | null;
  position_status_label?: string;
  position_status_tone?: PositionStatusTone;
  outcome_reason?: string | null;
  exit_rule_reason?: string | null;
  entry_mode?: string | null;
  edge?: number | null;
  edge_lcb?: number | null;
  probability?: number | null;
  model_side_probability?: number | null;
  settlement_high_f?: number | null;
  initial_cost?: number | null;
  entry_fee_per_contract?: number | null;
  why_good?: string | null;
}

/** Open positions / pending limit orders share the ledger field shape; most
    numeric fields are nullable until the monitor marks them. */
export interface OpenPosition {
  id: number;
  ticker?: string;
  label?: string;
  side?: string;
  contracts?: number | null;
  entry_price?: number | null;
  limit_price?: number | null;
  risk?: number | null;
  risk_profile?: string;
  target_date?: string;
  filled_at?: string | null;
  cancelled_at?: string | null;
  expires_at?: string | null;
  account_id?: string | null;
  strategy_fingerprint?: string | null;
  sleeve?: string | null;
  fill_model?: string | null;
  current_bid?: number | null;
  current_value?: number | null;
  unrealized_pnl?: number | null;
  unrealized_roi?: number | null;
  quality_score?: number | null;
}

export interface MonitorAction {
  id: number;
  time: string;
  ticker: string;
  label: string;
  side: string;
  risk_profile: string;
  contracts: number | null;
  entry_price: number | null;
  exit_price: number | null;
  realized_pnl: number | null;
  realized_roi: number | null;
  note?: string | null;
  status?: string;
  target_date?: string;
  unrealized?: boolean;
}

export interface DayRow {
  date: string;
  cumulative_realized: number;
  realized_pnl?: number;
  opening_equity?: number | null;
  daily_realized_pnl?: number;
  closing_equity?: number | null;
  opening_attributed_pnl?: number;
  closing_attributed_pnl?: number;
  trades_opened?: number;
  opened?: number;
  closed?: number;
  resolved?: number;
  wins?: number;
  losses?: number;
  hit_rate?: number | null;
  signals?: number;
  approved_signals?: number;
  opened_spend?: number;
  resolved_spend?: number;
  settled?: number;
  roi?: number | null;
  forecast_predicted_high_f?: number | null;
  forecast_actual_high_f?: number | null;
  forecast_error_f?: number | null;
}

export interface WinnerLoser {
  label: string;
  side: string;
  ticker: string;
  target_date: string;
  realized_pnl: number;
  quality_score: number;
}

/* ---- AWS runtime sections (the subset the lab renders) ---- */

export interface RejectionReason {
  reason: string;
  count: number;
}
export interface ProfileGateStats {
  risk_profile: string;
  approved?: number;
  signals?: number;
  rejection_categories?: Record<string, number>;
  top_rejections?: RejectionReason[];
  top_rejections_all?: RejectionReason[];
}
export interface GateBehavior extends DeferrableSection {
  approved?: number;
  rejected?: number;
  by_profile?: ProfileGateStats[];
  top_rejections?: RejectionReason[];
  top_rejections_all?: RejectionReason[];
}

export interface SideStats {
  trades: number;
  wins: number;
  losses: number;
  hit_rate: number | null;
  realized_pnl: number;
  roi: number | null;
  capital: number;
}

export interface ProfilePaperSummary {
  closed_positions: number;
  win_count: number;
  loss_count: number;
  hit_rate: number | null;
  realized_pnl: number;
  roi: number | null;
  open_positions: number;
  pending_limit_orders?: number;
  open_risk?: number | null;
  latest_monitor_action_at?: string | null;
  latest_opened_at?: string | null;
}
export interface ProfileStatus {
  alert_level?: string;
  latest_signal_count?: number;
  paper_trading_status?: string;
  realized_pnl?: number;
  /** Entry scanner state, e.g. "live paused: daily loss …; recording near-misses only". */
  entry_scanner_reason?: string | null;
}
/** Per-profile slice of the daily summary (same shapes as the combined one). */
export interface ProfileDailySummary {
  totals?: {
    realized_pnl?: number;
    cumulative_realized_pnl?: number;
    roi?: number | null;
    hit_rate?: number | null;
    wins?: number;
    losses?: number;
    trades_closed?: number;
    capital_resolved?: number;
  };
  days?: DayRow[];
  window_start?: string;
  window_end?: string;
  opening_attributed_pnl?: number;
  current_attributed_pnl?: number;
  exit_reasons?: Record<string, number>;
  side_performance?: Record<string, SideStats>;
  window_days?: number;
  equity_available?: boolean;
  equity_basis?: "account_equity" | "attribution_only";
  equity_unavailable_reason?: string;
  current_equity?: number | null;
  starting_bankroll?: number | null;
  bankroll?: number | null;
}

/** Optional research-target evidence published by the AWS paper runtime. Every
    field is optional so an older or partially refreshed artifact remains safe
    to render while the three-account schema rolls forward. */
export interface ResearchDailyTarget {
  available?: boolean;
  reason?: string;
  account_id?: string;
  sleeve?: string;
  policy_version?: string;
  timezone?: string;
  metric?: string;
  objective_day?: string | null;
  realized_pnl?: number | null;
  target_pnl?: number | null;
  remaining_pnl?: number | null;
  achieved?: boolean;
  locked?: boolean;
  status?: string;
  observed_days?: number;
  zero_activity_days?: number;
  zero_pnl_days?: number;
  hit_count?: number;
  attainment_rate?: number | null;
  mean_daily_pnl?: number | null;
  median_daily_pnl?: number | null;
  p25_daily_pnl?: number | null;
  p75_daily_pnl?: number | null;
  daily_pnl_stddev?: number | null;
  day_cluster_bootstrap_95_ci?: {
    lower?: number | null;
    upper?: number | null;
    method?: string;
    samples?: number;
  } | null;
  maximum_drawdown_dollars?: number | null;
  maximum_drawdown_pct?: number | null;
  log_growth?: number | null;
  log_growth_per_day?: number | null;
  logical_decisions?: number;
  resolved_lots?: number;
  resolution_days?: number;
  independent_city_target_days?: number;
  lead_split?: Record<string, unknown>;
  execution?: Record<string, unknown>;
  exit_breakdown?: Record<string, unknown>;
  target_feasible?: boolean | null;
  feasibility_evidence?: string | null;
  available_conservative_expected_profit?: number | null;
  days?: Array<{
    objective_day?: string;
    realized_pnl?: number;
    target_pnl?: number;
    remaining_pnl?: number;
    achieved?: boolean;
    locked?: boolean;
  }>;
  disclaimer?: string;
}

export interface ProfileEntry {
  label: string;
  risk_profile: string;
  profile_type: string; // "primary" | "experimental"
  /** Archived books remain inspectable but never participate in new entries. */
  archived?: boolean;
  account_key?: string;
  learnings?: string[];
  recommended_changes?: string[];
  paper_trading?: { available?: boolean; summary?: ProfilePaperSummary };
  daily_summary?: ProfileDailySummary;
  daily_target?: ResearchDailyTarget | null;
  excluded_from?: string[];
  signal_quality?: SignalQuality;
  status?: ProfileStatus;
}

export interface ReadinessCheck {
  name: string;
  label: string;
  detail: string;
  passed: boolean;
  progress: number; // 0..1
  evidence_boundary?: string | null;
  source_cohort?: string | null;
}
export interface RealMoneyReadiness extends DeferrableSection {
  available: boolean;
  verdict?: string;
  status?: string;
  /** Published beside `status` when the checklist itself is deferred. */
  status_reasons?: string[];
  summary?: string;
  ready?: boolean;
  readiness_pct?: number;
  checks_passed?: number;
  checks_total?: number;
  checks?: ReadinessCheck[];
  evidence_boundary?: string | null;
  post_boundary_settlement_days?: number;
  source_cohort?: string | null;
  pilot_loss_remaining?: number;
  live_policy?: {
    enabled?: boolean;
    dry_run?: boolean;
    risk_capital?: number;
    pilot_max_loss_pct?: number;
    daily_loss_pct?: number;
    per_trade_risk_pct?: number;
    per_trade_risk?: number;
    daily_loss?: number;
    pilot_max_loss?: number;
  };
}

export interface ScatterPoint {
  x: number; // market-implied probability
  y: number; // model probability
  r: number;
  side: string;
  label: string;
  approved: boolean;
}
export interface EdgeBucket {
  range: string;
  avg_edge: number;
  count: number;
}
export interface CountBucket {
  range: string;
  count: number;
}
export interface SignalQuality {
  available: boolean;
  latest_target_date?: string;
  stale_candidates_filtered?: number;
  charts?: {
    probability_vs_market?: ScatterPoint[];
    edge_by_market_bucket?: EdgeBucket[];
    quality_distribution?: CountBucket[];
  };
  latest_candidates_by_profile?: Record<string, unknown[]>;
}

export interface HealthAlert {
  code?: string;
  title: string;
  detail?: string;
  level?: string;
  action?: string;
}
export interface EmosTarget {
  target_date: string;
  mu_f: number;
  sigma_f: number;
  n_models: number;
  latest_age_hours: number;
  lead_days: number;
}
export interface NwpTarget {
  target_date: string;
  model_count: number;
  lead_days: number;
}
export interface ForecastHealth {
  available: boolean;
  clisfo?: { available: boolean; rows?: number; lag_days?: number; max_lag_days?: number; latest_date?: string };
  nws_ground_truth?: { available: boolean; rows?: number; lag_days?: number; latest_date?: string };
  emos?: { available: boolean; live_targets?: EmosTarget[]; max_stale_hours?: number };
  nwp?: { available: boolean; recent_targets?: NwpTarget[]; min_healthy_models?: number; max_stale_hours?: number };
  warnings?: HealthAlert[];
}

export interface CalibrationSide {
  available: boolean;
  role?: string;
  source?: string;
  reason?: string;
  brier_score?: number;
  brier_skill?: number;
  ranked_probability_score?: number;
  ranked_probability_skill?: number;
  top_bin_accuracy?: number;
  log_loss?: number;
  sample_size?: number;
  outcome_count?: number;
}
export interface CalibrationComparison {
  active?: CalibrationSide;
  challenger?: CalibrationSide;
  comparison?: { label?: string; recommendation?: string; winner?: string };
}

export interface BacktestMetrics {
  approval_rate?: number;
  approved_hit_rate?: number;
  approved_roi?: number;
  approved_paper_pnl?: number;
  approved_capital_at_risk?: number;
  avg_quality?: number;
  brier_score?: number;
  log_loss?: number;
  hit_rate?: number;
}

export interface ProfileResolvedStats {
  resolved: number;
  wins: number;
  losses: number;
  hit_rate: number | null;
  realized_pnl: number;
  roi: number | null;
  capital_resolved: number;
}

export interface AccountSnapshot {
  account_id: string;
  role: string;
  status?: "ACTIVE" | "ARCHIVED" | "ARCHIVED_SETTLING" | string;
  created_at?: string;
  cutover_note?: string | null;
  initial_equity: number;
  cash_balance: number;
  available_cash: number;
  reservations: number;
  open_cost_basis: number;
  open_positions?: number;
  pending_limit_orders?: number;
  pending_limit_risk?: number;
  realized_equity: number;
  realized_pnl: number;
  unrealized_pnl: number | null;
  marked_equity: number | null;
  mark_coverage: string;
  reconciliation_status: string;
  verification_scope?: string;
  days?: DayRow[];
}

export interface StrategyLab {
  schema_version?: number;
  available: boolean;
  reason?: string;
  mode: string;
  live_orders_enabled?: boolean;
  disclaimer?: string;
  generated_at?: string;
  analysis_generated_at?: string | null;
  analysis_source_sha?: string | null;
  publication_mode?: "fast_public" | "full_research";
  default_profile?: string;
  source_of_truth?: string;
  accounting?: {
    schema_version?: number;
    available?: boolean;
    reason?: string;
    initial_capital: number;
    all_time_realized_pnl: number;
    window_realized_pnl: number;
    realized_equity: number;
    cash_balance: number;
    reservations: number;
    available_cash: number;
    open_cost_basis: number;
    unrealized_pnl: number | null;
    marked_equity: number | null;
    mark_coverage: string;
    resolved_capital: number;
    return_on_initial_capital: number | null;
    roi_on_resolved_capital: number | null;
    reconciliation_status: string;
    accounting_cohort?: string;
    accounts?: Record<string, AccountSnapshot>;
    active_ledgers?: Record<string, AccountSnapshot>;
    archived_accounts?: Array<AccountSnapshot & {
      key?: string;
      label?: string;
      profile_key?: string;
      attribution_profile_key?: string;
      best_day_pnl?: number | null;
      days_at_or_above_8?: number;
      wins?: number;
      losses?: number;
    }>;
    combined?: Record<string, unknown> | null;
    goal?: {
      metric: string;
      account_id: string;
      timezone: string;
      period_start: string;
      period_end: string;
      target_return: number;
      starting_realized_equity: number;
      current_realized_equity: number;
      weekly_realized_pnl: number;
      weekly_realized_return: number | null;
      target_realized_pnl: number;
      remaining_pnl: number;
      achieved: boolean;
      completed_week_success_streak: number;
      evidence_boundary: string | null;
      first_full_evidence_week: string | null;
      current_week_evidence_qualified: boolean;
      execution_model_version: string;
      excludes: string[];
      disclaimer: string;
    };
  };
  paper_trading: {
    available: boolean;
    summary: {
      realized_pnl: number;
      roi: number;
      hit_rate: number;
      closed_positions: number;
      win_count: number;
      loss_count: number;
      capital_at_risk: number;
      open_positions: number;
      open_risk?: number;
      pending_limit_orders?: number;
      pending_limit_risk?: number;
      latest_monitor_action_at?: string | null;
    };
    closed_positions: ClosedPosition[];
    open_positions?: OpenPosition[];
    pending_limit_orders?: OpenPosition[];
    recent_monitor_actions?: MonitorAction[];
    monitor?: Record<string, number>;
    diagnostics?: { by_profile?: Record<string, ProfileResolvedStats> };
  };
  daily_summary: {
    available: boolean;
    equity_available?: boolean;
    equity_basis?: "account_equity" | "attribution_only";
    equity_unavailable_reason?: string;
    current_equity: number | null;
    starting_bankroll: number | null;
    window_days?: number;
    window_start?: string;
    window_end?: string;
    totals: {
      realized_pnl: number;
      cumulative_realized_pnl: number;
      hit_rate: number;
      roi: number;
      wins: number;
      losses: number;
      trades_closed: number;
      mean_abs_forecast_error_f: number;
    };
    days: DayRow[];
    biggest_winners: WinnerLoser[];
    biggest_losers: WinnerLoser[];
    learnings: string[];
    recommended_changes: string[];
    exit_reasons?: Record<string, number>;
    side_performance?: Record<string, SideStats>;
    data_collected?: Record<string, number>;
    decision_analytics?: {
      status?: "fresh" | "cached" | "deferred" | string;
      analysis_generated_at?: string | null;
      counts_stale_from?: string | null;
      reason?: string | null;
    };
    model_vs_market?: { samples?: number; mean_abs_gap?: number; max_abs_gap?: number };
    gate_behavior?: GateBehavior;
  };
  backtest_summary: {
    available: boolean;
    /** Why the historical analytics are absent when `available` is false. */
    reason?: string;
    counts: Record<string, number>;
    metrics?: BacktestMetrics;
    metrics_available?: boolean;
    dedupe_explanation?: string;
  };
  research_notes: { term: string; note: string }[];
  research_daily_target?: ResearchDailyTarget;
  profiles?: ProfileEntry[];
  real_money_readiness?: RealMoneyReadiness;
  signal_quality?: SignalQuality;
  forecast_health?: ForecastHealth;
  calibration_comparison?: CalibrationComparison;
  status?: {
    automation_status?: string;
    alerts?: HealthAlert[];
    aws_execution_calibration_locked?: boolean;
    target_exposure_cap?: number;
    max_entries_per_market_side?: number;
    bankroll?: number;
    last_updated?: string;
  };
}

export const useStrategyLab = () => useResource<StrategyLab>("strategy_research.json");

const ACTIVE_PROFILE_ORDER = ["live", "research-target"] as const;

/** Canonical account projection for every public Strategy Lab surface. Legacy
    research is a fallback only when neither of the new research sleeves is in
    the artifact; this deliberately never invents an empty research book. */
export function activeProfiles(s: StrategyLab): ProfileEntry[] {
  if (s.accounting?.available === false) return [];

  const unique = new Map<string, ProfileEntry>();
  for (const profile of s.profiles ?? []) {
    if (profile?.risk_profile && !unique.has(profile.risk_profile)) {
      unique.set(profile.risk_profile, profile);
    }
  }

  const ordered = ACTIVE_PROFILE_ORDER.flatMap((name) => {
    const profile = unique.get(name);
    return profile && !profile.archived ? [profile] : [];
  });
  if (!unique.has("research-target")) {
    const legacy = unique.get("research");
    if (legacy && !legacy.archived) ordered.push(legacy);
  }
  return ordered;
}

const ARCHIVED_PROFILE_ORDER = [
  "live-legacy",
  "research-target-v1",
  "research-target-v2",
  "research-target-v3",
  "research-target-v4",
  "research-target-v5",
  "research-motion",
  "research",
] as const;

/** Prior books stay visible as achieved performance without leaking into the
    two active ledgers or their readiness/objective calculations. */
export function archivedProfiles(s: StrategyLab): ProfileEntry[] {
  const rows = new Map(
    (s.profiles ?? [])
      .filter((profile) => profile?.risk_profile)
      .map((profile) => [profile.risk_profile, profile] as const),
  );
  const known = new Set<string>(ARCHIVED_PROFILE_ORDER);
  return [...rows.values()]
    .filter((profile) => profile.archived === true || known.has(profile.risk_profile))
    .sort((a, b) => {
      const ai = ARCHIVED_PROFILE_ORDER.indexOf(
        a.risk_profile as (typeof ARCHIVED_PROFILE_ORDER)[number],
      );
      const bi = ARCHIVED_PROFILE_ORDER.indexOf(
        b.risk_profile as (typeof ARCHIVED_PROFILE_ORDER)[number],
      );
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi)
        || a.risk_profile.localeCompare(b.risk_profile);
    });
}

const FEATURED_ARCHIVED_PROFILE_ORDER = [
  "live-legacy",
  "research-target-v1",
  "research-target-v3",
  "research-target-v4",
] as const;

/** Recruiter-facing research lineage. The full archive remains published and
    queryable, while this projection intentionally keeps the documented eras
    that changed the live/ROI design. */
export function featuredArchivedProfiles(s: StrategyLab): ProfileEntry[] {
  const archived = new Map(
    archivedProfiles(s).map((profile) => [profile.risk_profile, profile] as const),
  );
  return FEATURED_ARCHIVED_PROFILE_ORDER.flatMap((name) => {
    const profile = archived.get(name);
    return profile ? [profile] : [];
  });
}

const PROFILE_DISPLAY_LABELS: Record<string, string> = {
  live: "Live Stability",
  "live-legacy": "Prior readiness benchmark",
  research: "Legacy research",
  "research-target": "Research ROI",
  "research-target-v1": "Research ROI v1 · archived control",
  "research-target-v2": "Research ROI v2 · archived",
  "research-target-v3": "Research ROI v3 · rejected",
  "research-target-v4": "Research ROI v4 · adopted revision",
  "research-target-v5": "Research ROI v5 · superseded probe",
  "research-motion": "Research motion · archived",
};

/** Stable recruiter-facing identity. Runtime labels remain available in the
    raw artifact, but presentation does not inherit obsolete KPI or "live"
    wording from an older publisher. */
export function profileDisplayLabel(profile: Pick<ProfileEntry, "risk_profile" | "label">): string {
  return PROFILE_DISPLAY_LABELS[profile.risk_profile] ?? profile.label;
}

/** Resolve the target evidence from the profile first, then the top-level
    compatibility field. An explicit unavailable marker behaves like absence. */
export function researchDailyTarget(
  s: StrategyLab,
  profile?: ProfileEntry,
): ResearchDailyTarget | undefined {
  const artifact = profile?.daily_target ?? s.research_daily_target;
  return artifact?.available === false ? undefined : artifact ?? undefined;
}

/** Equity curve from any day series + starting bankroll (combined book OR a
    single profile). Tolerant of missing dates / cumulative values. */
export function equitySeriesFromDays(days: DayRow[] | undefined, startingBankroll = 1000) {
  return [...(days ?? [])]
    .filter((d) => !!d?.date)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((d) => {
      const accountBalance =
        typeof d.closing_equity === "number" && Number.isFinite(d.closing_equity)
          ? Math.round(d.closing_equity * 100) / 100
          : undefined;
      return {
        date: d.date.slice(5), // MM-DD
        equity: accountBalance ?? Math.round(
          (startingBankroll + (d.cumulative_realized ?? 0)) * 100,
        ) / 100,
        pnl: d.cumulative_realized ?? 0,
        dailyPnl: d.realized_pnl ?? 0,
        ...(accountBalance == null ? {} : { accountBalance }),
      };
    });
}

/** Equity curve across the reporting window: starting bankroll + cumulative realized. */
export function equitySeries(s: StrategyLab) {
  const startingBankroll =
    s.daily_summary?.equity_basis === "attribution_only"
      ? 0
      : (s.daily_summary?.starting_bankroll ?? 1000);
  return equitySeriesFromDays(s.daily_summary?.days, startingBankroll);
}

/** When a position actually resolved. A trade held to settlement carries
    `settled_at` and never `closed_at`, so ordering on `closed_at` alone sorts
    every settlement to the bottom as if it were undated. Mirrors the runtime's
    own resolution chain in `paper_card.py`. */
export function resolutionTime(p: ClosedPosition): string {
  return p.settled_at ?? p.closed_at ?? p.cancelled_at ?? "";
}

/** Full closed ledger, newest resolution first. */
export function closedLedger(s: StrategyLab): ClosedPosition[] {
  return [...(s.paper_trading?.closed_positions ?? [])].sort((a, b) =>
    resolutionTime(b).localeCompare(resolutionTime(a)),
  );
}

/** Gate stats for one risk profile (matched from daily_summary.gate_behavior). */
export function profileGate(s: StrategyLab, riskProfile: string): ProfileGateStats | undefined {
  return s.daily_summary?.gate_behavior?.by_profile?.find((g) => g.risk_profile === riskProfile);
}

export function gateCounts(gate: GateBehavior | undefined) {
  const approved = gate?.approved ?? 0;
  const rejected = gate?.rejected ?? 0;
  return { approved, rejected, total: approved + rejected };
}

/** True when the gate section carries no measurement: the bounded public
    refresh emits `{approved: 0, rejected: 0, by_profile: []}`, which means "not
    evaluated in this artifact", not "nothing survived". An explicit
    `available: false` is authoritative; today's artifact omits the flag, so the
    empty shape counts too. */
export function gateDeferred(gate: GateBehavior | undefined): boolean {
  if (!gate || gate.available === false) return true;
  if (gateCounts(gate).total > 0) return false;
  return !(gate.by_profile ?? []).some((g) => (g.signals ?? 0) > 0 || (g.approved ?? 0) > 0);
}

export function profileGateCounts(gate: ProfileGateStats | undefined) {
  return { approved: gate?.approved ?? 0, signals: gate?.signals ?? 0 };
}

/* ---- per-profile slices (all client-side; every ledger row carries risk_profile) ---- */

export const findProfile = (s: StrategyLab, rp: string): ProfileEntry | undefined =>
  s.profiles?.find((p) => p.risk_profile === rp);

/** Closed positions for one book. The published ledger contains the current
    calendar-month slice; the all-time count lives in paper_trading.summary. */
export function ledgerForProfile(s: StrategyLab, rp: string): ClosedPosition[] {
  return closedLedger(s).filter((p) => p.risk_profile === rp);
}
export function openForProfile(s: StrategyLab, rp: string): OpenPosition[] {
  return (s.paper_trading?.open_positions ?? []).filter((p) => p.risk_profile === rp);
}
export function pendingForProfile(s: StrategyLab, rp: string): OpenPosition[] {
  return (s.paper_trading?.pending_limit_orders ?? []).filter((p) => p.risk_profile === rp);
}
export function monitorForProfile(s: StrategyLab, rp: string): MonitorAction[] {
  return (s.paper_trading?.recent_monitor_actions ?? []).filter((a) => a.risk_profile === rp);
}

/* ---- multi-city lens: group a ledger by the market's city ---- */

export interface CityLedgerGroup {
  slug: string;
  name: string;
  trades: number;
  pnl: number;
  wins: number;
}
/** Roll a set of closed positions up by settlement city (via cityForTicker). */
export function ledgerByCity(rows: ClosedPosition[]): CityLedgerGroup[] {
  const map = new Map<string, CityLedgerGroup>();
  for (const r of rows) {
    const c = cityForTicker(r.ticker ?? "");
    const slug = c?.slug ?? "—";
    const g = map.get(slug) ?? { slug, name: c?.name ?? "Unknown", trades: 0, pnl: 0, wins: 0 };
    g.trades += 1;
    g.pnl += r.realized_pnl ?? 0;
    if ((r.realized_pnl ?? 0) > 0) g.wins += 1;
    map.set(slug, g);
  }
  return [...map.values()].sort((a, b) => b.trades - a.trades || b.pnl - a.pnl);
}

export interface MoneyFormatOptions {
  digits?: number;
  /** always: +$1 / −$1 / +$0; except-zero: +$1 / −$1 / $0; negative-only: $1 / −$1 / $0 */
  sign?: "always" | "except-zero" | "negative-only";
}

/** Canonical USD formatter for every strategy surface (true minus sign). */
export function money(
  n: number | null | undefined,
  options: MoneyFormatOptions | number = {},
): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const normalized = typeof options === "number" ? { digits: options } : options;
  const digits = normalized.digits ?? 2;
  const sign = normalized.sign ?? "always";
  const magnitude = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Math.abs(n));
  const prefix = n < 0 ? "−" : sign === "always" || (sign === "except-zero" && n > 0) ? "+" : "";
  return `${prefix}${magnitude}`;
}

/** Contract price as cents: 0.92 → 92¢ (true minus sign for defensive negative input). */
export const cents = (p: number | null | undefined) =>
  p == null || Number.isNaN(p) ? "—" : `${p < 0 ? "−" : ""}${Math.round(Math.abs(p) * 100)}¢`;
