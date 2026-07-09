import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Target } from "../../lib/data";
import { TargetStatusWarning } from "./TargetStatusWarning";

const target = (target_date: string, target_status: Target["target_status"]) =>
  ({ target_date, target_status }) as Target;

describe("TargetStatusWarning", () => {
  it("archives past-due targets instead of presenting them as active", () => {
    render(
      <TargetStatusWarning
        targets={[target("2026-07-07", "past"), target("2026-07-09", "settlement_day")]}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/1 past-due prediction-market target/i);
    expect(screen.getByRole("alert")).toHaveTextContent(/archived and excluded from current status/i);
  });

  it("raises a critical warning when every published target is past due", () => {
    render(<TargetStatusWarning targets={[target("2026-07-07", "past")]} />);

    expect(screen.getByRole("alert")).toHaveTextContent(/no settlement-day or upcoming target is published/i);
  });
});
