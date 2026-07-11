import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { vi } from "vitest";
import type { ReactNode } from "react";
import type { ForecastData, TradingSignal } from "../../lib/data";
import { SkillStrip } from "./SkillStrip";

vi.mock("@heroui/react/card", () => {
  const Card = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  Card.Content = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  return { Card };
});

vi.mock("@heroui-pro/react/kpi", () => {
  const KPI = ({ children }: { children: ReactNode }) => <section>{children}</section>;
  KPI.Header = ({ children }: { children: ReactNode }) => <header>{children}</header>;
  KPI.Title = ({ children }: { children: ReactNode }) => <span>{children}</span>;
  KPI.Content = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  KPI.Progress = () => <div />;
  return { KPI };
});

vi.mock("@heroui-pro/react/kpi-group", () => {
  const KPIGroup = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  KPIGroup.Separator = () => <hr />;
  return { KPIGroup };
});

vi.mock("../ui/Reveal", () => ({
  Reveal: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

describe("SkillStrip", () => {
  it("renders unavailable values when calibration and observation counts are missing", () => {
    render(<SkillStrip forecast={{ n_years: 10 } as ForecastData} signal={{} as TradingSignal} />);

    expect(screen.getByText("Brier skill")).toBeInTheDocument();
    expect(screen.getByText("History")).toBeInTheDocument();
    expect(screen.getByText("— days")).toBeInTheDocument();
  });
});
