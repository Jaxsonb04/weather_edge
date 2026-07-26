import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { ResearchDailyTarget } from "../../lib/strategy";

vi.mock("@iconify/react/offline", () => ({ Icon: () => null }));
vi.mock("@heroui/react/card", () => {
  const Part = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const Card = Part as typeof Part & {
    Header: typeof Part;
    Content: typeof Part;
    Title: typeof Part;
  };
  Card.Header = Part;
  Card.Content = Part;
  Card.Title = Part;
  return { Card };
});
vi.mock("@heroui/react/chip", () => {
  const Part = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const Chip = Part as typeof Part & { Label: typeof Part };
  Chip.Label = Part;
  return { Chip };
});

import { DailyTargetEvidence } from "./ProfileDashboard";

describe("DailyTargetEvidence", () => {
  it("does not invent the retired $50 objective when a tolerant artifact omits it", () => {
    render(
      <DailyTargetEvidence
        target={{ realized_pnl: 4 } as ResearchDailyTarget}
      />,
    );

    expect(screen.getByText(/of objective unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/Objective amount unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/\$50\.00/)).not.toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("renders the active policy objective exactly as published", () => {
    render(
      <DailyTargetEvidence
        target={{
          target_pnl: 16,
          realized_pnl: 8,
          remaining_pnl: 8,
        } as ResearchDailyTarget}
      />,
    );

    expect(screen.getByText(/\$8\.00 of \$16\.00/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuemax", "16");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "8");
  });
});
