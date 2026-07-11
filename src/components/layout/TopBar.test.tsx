import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@iconify/react/offline", () => ({ Icon: () => null }));

import { TopBar } from "./TopBar";

const props = {
  mode: "dark" as const,
  onToggleTheme: vi.fn(),
  onOpenCommand: vi.fn(),
  route: "overview" as const,
  repoUrl: "https://example.com/source",
  liveUrl: "https://example.com/live",
};

describe("TopBar mobile menu keyboard behavior", () => {
  it("keeps brand and source link names explicit when responsive text is hidden", () => {
    render(<TopBar {...props} />);

    expect(screen.getByRole("link", { name: "WeatherEdge overview" })).toHaveAttribute(
      "aria-label",
      "WeatherEdge overview",
    );
    expect(screen.getByRole("link", { name: "WeatherEdge source on GitHub" })).toHaveAttribute(
      "aria-label",
      "WeatherEdge source on GitHub",
    );
  });

  it("focuses the first link on open and restores the trigger on Escape", () => {
    render(<TopBar {...props} />);
    const trigger = screen.getByRole("button", { name: "Open menu" });

    fireEvent.click(trigger);
    const menu = screen.getByRole("navigation", { name: "Mobile navigation" });
    expect(within(menu).getByRole("link", { name: "Overview" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("navigation", { name: "Mobile navigation" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("wraps keyboard focus inside the open mobile menu", () => {
    render(<TopBar {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));

    const menu = screen.getByRole("navigation", { name: "Mobile navigation" });
    const first = within(menu).getByRole("link", { name: "Overview" });
    const last = within(menu).getByRole("link", { name: "Source on GitHub" });
    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(first).toHaveFocus();

    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();
  });
});
