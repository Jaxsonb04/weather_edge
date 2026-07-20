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
    } as StrategyLab;

    render(<ReadinessPanel s={strategy} />);

    expect(screen.getByText("NOT READY")).toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: /Go-live checklist/i });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });
});
