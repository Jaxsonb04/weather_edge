import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StrategyLab } from "../../lib/strategy";
import { ReadinessPanel } from "./ReadinessPanel";

// Regression: long technical lists should not crowd the overall conclusion.
describe("ReadinessPanel progressive detail", () => {
  it("keeps the verdict visible and folds a checklist longer than five rows", () => {
    const strategy = {
      real_money_readiness: {
        available: true,
        ready: false,
        verdict: "NOT READY",
        checks_passed: 1,
        checks_total: 6,
        checks: Array.from({ length: 6 }, (_, index) => ({
          name: `check-${index}`,
          label: `Check ${index + 1}`,
          detail: "Technical evidence",
          passed: index === 0,
          progress: index === 0 ? 1 : 0,
        })),
      },
    } as unknown as StrategyLab;

    render(<ReadinessPanel s={strategy} />);

    expect(screen.getByText("NOT READY")).toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: /Go-live checklist/i });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the bankroll-relative basis for future-live limits", () => {
    const strategy = {
      real_money_readiness: {
        available: true,
        ready: false,
        verdict: "NOT READY",
        checks: [],
        pilot_loss_remaining: 125,
        live_policy: {
          enabled: false,
          dry_run: true,
          risk_capital: 2500,
          pilot_max_loss_pct: 0.05,
          daily_loss_pct: 0.02,
          per_trade_risk_pct: 0.01,
          pilot_max_loss: 125,
          daily_loss: 50,
          per_trade_risk: 25,
        },
      },
    } as unknown as StrategyLab;

    render(<ReadinessPanel s={strategy} />);

    expect(screen.getByText("Risk capital")).toBeInTheDocument();
    expect(screen.getByText("$2,500.00")).toBeInTheDocument();
    expect(screen.getByText("$25.00 · 1%")).toBeInTheDocument();
    expect(screen.getByText("$50.00 · 2%")).toBeInTheDocument();
    expect(
      screen.getByText(/5% of the configured risk capital \(\$125\.00\)/i),
    ).toBeInTheDocument();
  });

  it("states the kill-switch limit once, then what is left", () => {
    const strategy = (remaining: number) =>
      ({
        real_money_readiness: {
          available: true,
          ready: false,
          verdict: "NOT READY",
          checks: [],
          pilot_loss_remaining: remaining,
          live_policy: { enabled: false, dry_run: true, pilot_max_loss_pct: 0.05, pilot_max_loss: 50 },
        },
      }) as unknown as StrategyLab;

    const { container, unmount } = render(<ReadinessPanel s={strategy(50)} />);
    expect(container.textContent).toContain("5% of the configured risk capital ($50.00); none used yet.");
    expect(container.textContent?.match(/\$50\.00/g)).toHaveLength(1);
    unmount();

    const used = render(<ReadinessPanel s={strategy(35)} />);
    expect(used.container.textContent).toContain("($50.00); $35.00 remaining.");
  });

  it("labels stale deploy-time analysis without implying newly failed checks", () => {
    const strategy = {
      real_money_readiness: {
        available: false,
        ready: false,
        status: "ANALYSIS_STALE",
        reason: "Historical analysis is stale.",
      },
    } as unknown as StrategyLab;

    render(<ReadinessPanel s={strategy} />);

    expect(screen.getByText("Analysis not refreshed")).toBeInTheDocument();
    expect(screen.getByText(/does not mean the checks newly failed/i)).toBeInTheDocument();
  });

  it("never calls live orders disabled when the artifact omits the flag", () => {
    const readiness = { available: false, ready: false, status: "ANALYSIS_STALE", reason: "Historical analysis is stale." };
    const cases: Array<[boolean | undefined, string]> = [
      [undefined, "Live-order flag not published"],
      [false, "Live orders disabled"],
      [true, "Live orders enabled"],
    ];
    for (const [flag, label] of cases) {
      const strategy = { live_orders_enabled: flag, real_money_readiness: readiness } as unknown as StrategyLab;
      const { unmount } = render(<ReadinessPanel s={strategy} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      if (flag === undefined) expect(screen.queryByText("Live orders disabled")).not.toBeInTheDocument();
      unmount();
    }
  });
});
