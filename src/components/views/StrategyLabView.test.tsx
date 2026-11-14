import { act, render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { StrategyLab } from "../../lib/strategy";

vi.mock("../strategy/EquityCurve", () => ({
  EquityCurve: (props: { title?: string }) => <div data-testid="equity-curve">{props.title}</div>,
}));

vi.mock("../ui/Reveal", () => ({
  Reveal: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

import { AnalysisFreshness, LabNotices, LabStatusChips, PolicyLineage, ReleaseFindings } from "./StrategyLabView";
import { AccountCards } from "../strategy/AccountCards";
import { ReadinessPanel } from "../strategy/ReadinessPanel";
import { useHashSub } from "../../lib/useHashRoute";

vi.mock("../../lib/publication", () => ({ usePublication: () => ({ strategy: { state: "fresh" } }) }));

describe("Lab tab deep links", () => {
  const tabs = ["accounts", "research", "archive", "system"] as const;
  const go = (hash: string) =>
    act(() => {
      window.history.replaceState(null, "", hash);
      window.dispatchEvent(new Event("hashchange"));
    });

  it("follows hash navigation between tabs while the Lab stays open", () => {
    window.history.replaceState(null, "", "#/lab/research");
    const { result } = renderHook(() => useHashSub("lab", tabs, "accounts"));
    expect(result.current[0]).toBe("research");

    go("#/lab/system");
    expect(result.current[0]).toBe("system");
    go("#/lab");
    expect(result.current[0]).toBe("accounts");

    // Leaving the Lab is not a tab change.
    go("#/lab/archive");
    go("#/overview");
    expect(result.current[0]).toBe("archive");

    // Choosing a tab rewrites the hash without a hashchange (no jump to the top).
    const onHash = vi.fn();
    window.addEventListener("hashchange", onHash);
    act(() => result.current[1]("research"));
    window.removeEventListener("hashchange", onHash);
    expect(result.current[0]).toBe("research");
    expect(window.location.hash).toBe("#/lab/research");
    expect(onHash).not.toHaveBeenCalled();

    window.history.replaceState(null, "", "/");
  });
});

const twoAccounts = {
  available: true,
  mode: "paper_research_only",
  live_orders_enabled: false,
  schema_version: 3,
  generated_at: "2026-08-02T10:00:55+00:00",
  // Top-level compatibility copy of the research target; the readiness book must
  // never borrow its policy version.
  research_daily_target: { available: true, policy_version: "research-target-roi-v6" },
  accounting: {
    available: true,
    // Diagnostic-only total across separate accounts: must never be rendered.
    combined: { realized_equity: 2008.6, label: "Live plus research; never used for the weekly goal or readiness" },
  },
  profiles: [
    {
      label: "Live Stability",
      risk_profile: "live",
      profile_type: "primary",
      daily_summary: { current_equity: 1025.08, starting_bankroll: 1000 },
      paper_trading: {
        summary: { realized_pnl: 25.08, closed_positions: 37, win_count: 30, loss_count: 7, hit_rate: 0.81, open_positions: 2 },
      },
    },
    {
      label: "Research ROI",
      risk_profile: "research-target",
      profile_type: "experimental",
      daily_summary: { current_equity: 983.52, starting_bankroll: 1000 },
      daily_target: { available: true, policy_version: "research-target-roi-v6" },
      paper_trading: { summary: { realized_pnl: -16.48, closed_positions: 3 } },
    },
    {
      label: "Research ROI v5 (archived 1.5x step)",
      risk_profile: "research-target-v5",
      profile_type: "experimental",
      archived: true,
      paper_trading: { summary: { realized_pnl: -4.65, closed_positions: 2 } },
    },
    {
      label: "Research target v1 (archived control)",
      risk_profile: "research-target-v1",
      profile_type: "experimental",
      archived: true,
      paper_trading: { summary: { realized_pnl: 53.46, closed_positions: 76 } },
    },
  ],
  real_money_readiness: { available: true, checks_passed: 5, checks_total: 12 },
} as unknown as StrategyLab;

describe("AccountCards", () => {
  it("shows each paper account's balance separately and never a combined figure", () => {
    render(<AccountCards s={twoAccounts} selected="live" onSelect={() => {}} />);

    // The exact figure is read once by assistive tech; the animated digits are aria-hidden.
    expect(screen.getAllByText("$1,025.08").length).toBeGreaterThan(0);
    expect(screen.getAllByText("$983.52").length).toBeGreaterThan(0);
    // Only the research book runs a versioned policy; the readiness book never inherits it.
    expect(screen.getAllByText("Policy v6")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Research ROI v6/ })).toHaveTextContent("Policy v6");
    expect(screen.getByRole("button", { name: /Live Stability/ })).not.toHaveTextContent("Policy");
    expect(screen.queryByText(/2,008\.60/)).not.toBeInTheDocument();
    expect(screen.queryByText(/2,008\.6/)).not.toBeInTheDocument();
    // Archived accounts never join the active pair.
    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.getByRole("button", { name: /Live Stability/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("names the high-frequency research book by its published policy", () => {
    const profiles = twoAccounts.profiles?.map((p) =>
      p.risk_profile === "research-target"
        ? { ...p, daily_target: { available: true, policy_version: "research-target-hf-v8" } }
        : p);
    render(<AccountCards s={{ ...twoAccounts, profiles } as StrategyLab} selected="live" onSelect={() => {}} />);
    expect(screen.getByRole("button", { name: /High-frequency research v8/ })).toHaveTextContent("Policy v8");
  });

  it("fails closed when active paper accounting is unavailable", () => {
    render(
      <AccountCards
        s={{
          ...twoAccounts,
          accounting: { available: false, reason: "Fresh active paper ledgers failed reconciliation." },
        } as unknown as StrategyLab}
        selected="live"
        onSelect={() => {}}
      />,
    );

    expect(screen.getByRole("alert", { name: /paper account state unavailable/i })).toBeInTheDocument();
    expect(screen.getByText(/failed reconciliation/i)).toBeInTheDocument();
    expect(screen.queryByText("$1,025.08")).not.toBeInTheDocument();
    expect(screen.queryByText("$983.52")).not.toBeInTheDocument();
    expect(screen.queryByText(/Scanning/)).not.toBeInTheDocument();
    expect(screen.queryByText("Policy v6")).not.toBeInTheDocument();
  });

  it("fails closed when the publication artifact is unavailable", () => {
    render(
      <AccountCards
        s={{
          available: false,
          mode: "paper_research_only",
          reason: "Live Strategy data belongs on the runtime host after sync.",
          profiles: [
            { label: "Stale local profile", risk_profile: "live", profile_type: "primary", daily_summary: { current_equity: 1999.99 } },
          ],
        } as unknown as StrategyLab}
        selected="live"
        onSelect={() => {}}
      />,
    );

    expect(screen.getByRole("alert", { name: /paper account state unavailable/i })).toBeInTheDocument();
    expect(screen.getByText(/belongs on the runtime host/i)).toBeInTheDocument();
    expect(screen.queryByText("$1,999.99")).not.toBeInTheDocument();
    expect(screen.queryByText(/Scanning/)).not.toBeInTheDocument();
  });

  it("does not promote an archived legacy research profile into the active pair", () => {
    render(
      <AccountCards
        s={{
          available: true,
          mode: "paper_research_only",
          accounting: { available: true },
          profiles: [
            { label: "Live Stability", risk_profile: "live", profile_type: "primary", daily_summary: { current_equity: 1025 } },
            {
              label: "Archived research",
              risk_profile: "research",
              profile_type: "experimental",
              archived: true,
              daily_summary: { current_equity: 1888.88 },
              daily_target: { policy_version: "research-target-roi-v99" },
            },
          ],
        } as unknown as StrategyLab}
        selected="live"
        onSelect={() => {}}
      />,
    );

    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByText("$1,888.88")).not.toBeInTheDocument();
    expect(screen.queryByText("Policy v99")).not.toBeInTheDocument();
  });

  it("labels the paper hit rate as profitable exits, not a settlement hit rate", () => {
    render(<AccountCards s={twoAccounts} selected="live" onSelect={() => {}} />);
    expect(screen.getAllByText("Profitable exits").length).toBeGreaterThan(0);
    expect(screen.queryByText(/hit rate/i)).not.toBeInTheDocument();
  });
});

describe("LabStatusChips", () => {
  it("states paper-only execution and the publication time", () => {
    render(<LabStatusChips s={twoAccounts} />);
    expect(screen.getByText("Paper only · $0 real money")).toBeInTheDocument();
    expect(screen.getByText(/Updated Aug 2, 2026.*10:00 UTC/i)).toBeInTheDocument();
  });

  it("dates a stale deploy-time analysis plainly", () => {
    render(
      <LabStatusChips
        s={{
          ...twoAccounts,
          generated_at: "2026-11-01T22:00:52+00:00",
          analysis_generated_at: "2026-09-05T04:36:08+00:00",
          real_money_readiness: {
            available: false,
            status: "ANALYSIS_STALE",
            analysis_age_hours: 1385.41,
            max_analysis_age_hours: 36,
          },
        } as unknown as StrategyLab}
      />,
    );
    expect(screen.getByText("Analysis Sep 5, 2026 · 57 days old")).toBeInTheDocument();
  });

  it("never reports paper-only when the runtime says live orders are enabled", () => {
    render(<LabStatusChips s={{ ...twoAccounts, live_orders_enabled: true } as StrategyLab} />);
    expect(screen.getByText("Live orders enabled")).toBeInTheDocument();
    expect(screen.queryByText(/Paper only/)).not.toBeInTheDocument();
  });
});

describe("PolicyLineage", () => {
  it("lists the published research policy versions ending at the active one", () => {
    render(<PolicyLineage s={twoAccounts} />);
    expect(screen.getByText(/Superseded scale probe · n=2/)).toBeInTheDocument();
    expect(screen.getByText("Active research policy")).toBeInTheDocument();
    expect(screen.getByText("v6")).toBeInTheDocument();
  });
});

describe("PolicyLineage after an archive", () => {
  it("keeps a just-archived version in the trail (audit F28)", () => {
    const profiles = [
      { ...twoAccounts.profiles?.[1], daily_target: { available: true, policy_version: "research-target-roi-v7" } },
      { risk_profile: "research-target-v5", archived: true },
      { risk_profile: "research-target-v6", archived: true },
    ];
    render(<PolicyLineage s={{ ...twoAccounts, profiles } as StrategyLab} />);
    const versions = screen.getAllByRole("listitem").map((item) => item.textContent);
    expect(versions.map((text) => text?.match(/^v\d+/)?.[0])).toEqual(["v5", "v6", "v7"]);
  });
});

describe("LabNotices", () => {
  const alerts = [
    { code: "settlement-pending", level: "warning", title: "Settlement pending", detail: "Positions for 2026-11-06 (1) are awaiting final truth." },
    { code: "same-day-entry-blocked", level: "info", title: "Same-day entries blocked", detail: "live paper entry requires min_lead_days=1" },
    { code: "emos-live-missing", level: "warning", title: "Live EMOS target missing", detail: "KLAX has no live EMOS distribution for 2026-11-09; x" },
    { code: "emos-live-missing", level: "warning", title: "Live EMOS target missing", detail: "KSEA has no live EMOS distribution for 2026-11-09; x" },
  ];
  const lab = { ...twoAccounts, status: { alerts } } as unknown as StrategyLab;

  it("shows each notice only on the tab it affects, in plain words", () => {
    const { container, rerender } = render(<LabNotices s={lab} tab="accounts" />);
    expect(screen.getByText("Settlement pending")).toBeInTheDocument();
    expect(screen.getByText("Live and V8 only enter next-day markets, so same-day signals are research-only.")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/min_lead_days|EMOS/);

    rerender(<LabNotices s={lab} tab="system" />);
    expect(screen.getByText("KLAX, KSEA have no live forecast distribution yet and use a simpler fallback.")).toBeInTheDocument();
    expect(screen.queryByText("Settlement pending")).not.toBeInTheDocument();

    for (const tab of ["m1", "research", "archive"] as const) {
      rerender(<LabNotices s={lab} tab={tab} />);
      expect(container).toBeEmptyDOMElement();
    }
  });

  it("keeps other same-day entry failures as normal notices with their detail", () => {
    const s = {
      ...twoAccounts,
      status: {
        alerts: [{ code: "same-day-entry-blocked", level: "warning", title: "Same-day entries blocked", detail: "Live forecast failed to load." }],
      },
    } as unknown as StrategyLab;
    render(<LabNotices s={s} tab="accounts" />);
    expect(screen.getByText("Same-day entries blocked")).toBeInTheDocument();
    expect(screen.getByText("Live forecast failed to load.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Same-day entries blocked/ })).toBeInTheDocument();
    expect(screen.queryByText("Next-day entries only")).not.toBeInTheDocument();
  });

  it("keeps account drawdown and unmarked-position warnings on Accounts", () => {
    const s = {
      ...twoAccounts,
      status: {
        alerts: [
          { code: "live-drawdown", level: "warning", title: "Live drawdown", detail: "d" },
          { code: "open-positions-unmarked", level: "warning", title: "Positions unmarked", detail: "u" },
        ],
      },
    } as unknown as StrategyLab;
    const { rerender } = render(<LabNotices s={s} tab="accounts" />);
    expect(screen.getByText("Live drawdown")).toBeInTheDocument();
    expect(screen.getByText("Positions unmarked")).toBeInTheDocument();
    rerender(<LabNotices s={s} tab="system" />);
    expect(screen.queryByText("Live drawdown")).not.toBeInTheDocument();
  });

  it("summarizes a settlement backlog on Accounts and keeps a critical unknown alert on every tab", () => {
    const s = {
      ...twoAccounts,
      status: {
        alerts: [
          { code: "settlement-backlog", level: "critical", title: "Settlement backlog", detail: "Paper positions are up to 35 days past settlement.", action: "Run paper-auto-settle." },
          { code: "mystery", level: "critical", title: "Something broke", detail: "Check the runtime." },
        ],
      },
      paper_trading: { summary: { unresolved_past_targets: [{ open_orders: 3, target_date: "2026-09-27" }] } },
    } as unknown as StrategyLab;

    const { rerender } = render(<LabNotices s={s} tab="accounts" />);
    expect(screen.getAllByRole("alert")[0]).toHaveTextContent("3 open positions from Sep 27 have not settled");
    expect(screen.getByText("Something broke")).toBeInTheDocument();

    rerender(<LabNotices s={s} tab="m1" />);
    expect(screen.queryByText("Settlement backlog")).not.toBeInTheDocument();
    expect(screen.getByText("Something broke")).toBeInTheDocument();
  });

  it("stays silent when no alert is active, and never turns a finished outage into a notice", () => {
    const { container } = render(<LabNotices s={twoAccounts as unknown as StrategyLab} tab="accounts" />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("AnalysisFreshness", () => {
  it("separates a fresh live publication from older cached historical analysis", () => {
    render(
      <AnalysisFreshness
        s={{
          publication_mode: "fast_public",
          analysis_generated_at: "2026-07-25T08:00:00+00:00",
        } as StrategyLab}
      />,
    );

    expect(screen.getByText(/Historical rescore cached from 2026-07-25 08:00 UTC/i)).toBeInTheDocument();
    expect(screen.getByText(/paper balances and positions refresh with every publication/i)).toBeInTheDocument();
    expect(screen.getByText(/readiness evidence refreshes only with the deploy-time analysis/i)).toBeInTheDocument();
  });

  it("states when historical analysis is deferred instead of inventing freshness", () => {
    render(
      <AnalysisFreshness
        s={{
          publication_mode: "fast_public",
          analysis_generated_at: null,
        } as StrategyLab}
      />,
    );

    expect(screen.getByText(/Historical rescore deferred/i)).toBeInTheDocument();
  });
});

describe("readiness accounting gate", () => {
  it("suppresses the readiness surface when accounting is explicitly unavailable", () => {
    const s = {
      available: true,
      mode: "paper_research_only",
      accounting: {
        available: false,
        reason: "fresh active paper ledgers invalid or unavailable",
      },
      real_money_readiness: {
        available: true,
        ready: true,
        verdict: "READY",
        checks_passed: 8,
        checks_total: 8,
        checks: [],
      },
    } as unknown as StrategyLab;

    render(<ReadinessPanel s={s} />);

    expect(screen.queryByText(/8\/8 checks/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/real-money readiness/i)).not.toBeInTheDocument();
    expect(screen.queryByText("READY")).not.toBeInTheDocument();
  });
});

describe("published release findings", () => {
  it("links a dated source audit without claiming an old runtime has upgraded", () => {
    render(<ReleaseFindings s={{} as StrategyLab} />);
    expect(screen.getByText(/October 5, 2026 baseline/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /dated audit/i })).toHaveAttribute("href", "https://github.com/Jaxsonb04/weather_edge/blob/main/docs/research/2026-10-05-v7-audit.md");
    expect(screen.queryByText("V7 research audit")).not.toBeInTheDocument();
  });

  it("shows evidence and validation state without treating a correction as profit", () => {
    render(<ReleaseFindings s={{ release: {
      version: "v7",
      comparison_note: "v6 remains a separate account.",
      baseline_report_url: "https://github.com/Jaxsonb04/weather_edge/blob/main/docs/research/2026-10-05-v7-audit.md",
      findings: [{ code: "exit-depth", title: "Shared quote depth", evidence: "reproduced source defect", state: "tested correction", impact: "Prior lots could reuse the same bid depth." }],
    } } as StrategyLab} />);
    expect(screen.getByText("V7 research audit")).toBeInTheDocument();
    expect(screen.getByText("Evidence: reproduced source defect")).toBeInTheDocument();
    expect(screen.getByText("tested correction")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /dated baseline/i })).toHaveAttribute("href", "https://github.com/Jaxsonb04/weather_edge/blob/main/docs/research/2026-10-05-v7-audit.md");
  });
});
