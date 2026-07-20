import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DetailDisclosure } from "./DetailDisclosure";

// Regression: ISSUE-003 — dense research evidence had no collapsed state.
// Report: .gstack/qa-reports/qa-report-jaxsonb04-github-io-2026-07-20.md
describe("detail disclosure", () => {
  it("starts collapsed and exposes its evidence from the labeled arrow control", () => {
    render(
      <DetailDisclosure
        id="test-evidence"
        icon="solar:chart-square-bold"
        title="Signal evidence"
        note="Detailed profile diagnostics"
      >
        <p>Deep evidence</p>
      </DetailDisclosure>,
    );

    const trigger = screen.getByRole("button", { name: /Signal evidence/i });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Deep evidence")).toBeInTheDocument();
  });
});
