import { cityForTicker, useResource } from "./data";

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
  closed_at: string;
  position_status_label?: string;
  position_status_tone?: string;
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
  contracts: number;
  entry_price: number | null;
  exit_price: number | null;
  realized_pnl: number;
  realized_roi: number | null;
  note?: string | null;
  status?: string;
  target_date?: string;
}

export interface DayRow {
  date: string;
  cumulative_realized: number;
  realized_pnl?: number;
  trades_opened?: number;
  opened?: number;
  closed?: number;
  wins?: number;
  losses?: number;
  hit_rate?: number | null;
  signals?: number;
  approved_signals?: number;
  opened_spend?: number;
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
  approved: number;
  signals: number;
  rejection_categories?: Record<string, number>;
  top_rejections?: RejectionReason[];
  top_rejections_all?: RejectionReason[];
}
export interface GateBehavior {
  approved: number;
  rejected: number;
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
}
export interface ProfileStatus {
  alert_level?: string;
  latest_signal_count?: number;
  paper_trading_status?: string;
  realized_pnl?: number;
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
  exit_reasons?: Record<string, number>;
  side_performance?: Record<string, SideStats>;
  window_days?: number;
  current_equity?: number;
  starting_bankroll?: number;
  bankroll?: number;
}
export interface ProfileEntry {
  label: string;
  risk_profile: string;
  profile_type: string; // "primary" | "experimental"
  learnings?: string[];
  recommended_changes?: string[];
  paper_trading?: { available?: boolean; summary?: ProfilePaperSummary };
  daily_summary?: ProfileDailySummary;
  signal_quality?: SignalQuality;
  status?: ProfileStatus;
}

export interface ReadinessCheck {
  name: string;
  label: string;
  detail: string;
  passed: boolean;
  progress: number; // 0..1
}
export interface RealMoneyReadiness {
  available: boolean;
  verdict?: string;
  status?: string;
  summary?: string;
  ready?: boolean;
  readiness_pct?: number;
  checks_passed?: number;
  checks_total?: number;
  checks?: ReadinessCheck[];
  pilot_loss_remaining?: number;
  live_policy?: {
    enabled?: boolean;
    dry_run?: boolean;
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

export interface StrategyLab {
  available: boolean;
  mode: string;
  disclaimer?: string;
  generated_at?: string;
  default_profile?: string;
  source_of_truth?: string;
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
    current_equity: number;
    starting_bankroll: number;
    window_days?: number;
    window_start?: string;
    window_end?: string;
    totals: {
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
    model_vs_market?: { samples?: number; mean_abs_gap?: number; max_abs_gap?: number };
    gate_behavior?: GateBehavior;
  };
  backtest_summary: {
    available: boolean;
    counts: Record<string, number>;
    metrics?: BacktestMetrics;
    metrics_available?: boolean;
    dedupe_explanation?: string;
  };
  research_notes: { term: string; note: string }[];
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

/** Equity curve from any day series + starting bankroll (combined book OR a
    single profile). Tolerant of missing dates / cumulative values. */
export function equitySeriesFromDays(days: DayRow[] | undefined, startingBankroll = 1000) {
  return [...(days ?? [])]
    .filter((d) => !!d?.date)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((d) => ({
      date: d.date.slice(5), // MM-DD
      equity: Math.round((startingBankroll + (d.cumulative_realized ?? 0)) * 100) / 100,
      pnl: d.cumulative_realized ?? 0,
    }));
}

/** Equity curve across the reporting window: starting bankroll + cumulative realized. */
export function equitySeries(s: StrategyLab) {
  return equitySeriesFromDays(s.daily_summary?.days, s.daily_summary?.starting_bankroll ?? 1000);
}

/** Full closed ledger, newest first. */
export function closedLedger(s: StrategyLab): ClosedPosition[] {
  return [...(s.paper_trading?.closed_positions ?? [])].sort((a, b) =>
    (b.closed_at ?? "").localeCompare(a.closed_at ?? ""),
  );
}

/** Gate stats for one risk profile (matched from daily_summary.gate_behavior). */
export function profileGate(s: StrategyLab, riskProfile: string): ProfileGateStats | undefined {
  return s.daily_summary?.gate_behavior?.by_profile?.find((g) => g.risk_profile === riskProfile);
}

/* ---- per-profile slices (all client-side; every ledger row carries risk_profile) ---- */

export const findProfile = (s: StrategyLab, rp: string): ProfileEntry | undefined =>
  s.profiles?.find((p) => p.risk_profile === rp);

/** Recent closed positions for one book (the published ledger is a recent slice,
    not the full history — the all-time count lives in paper_trading.summary). */
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

/** Signed money string: +$1.23 / −$4.56 (true minus sign). */
export const money = (n: number | null | undefined, digits = 2) =>
  n == null || Number.isNaN(n) ? "—" : `${n >= 0 ? "+" : "−"}$${Math.abs(n).toFixed(digits)}`;

/** Contract price as cents: 0.92 → 92¢. */
export const cents = (p: number | null | undefined) => (p == null ? "—" : `${Math.round(p * 100)}¢`);
