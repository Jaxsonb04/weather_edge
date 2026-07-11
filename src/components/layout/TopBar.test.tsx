import { createEvent, fireEvent, render, screen, within } from "@testing-library/react";
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

  it("lets natural Tab order leave the non-modal mobile menu", () => {
    render(
      <>
        <TopBar {...props} />
        <button type="button">After navigation</button>
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));

    const menu = screen.getByRole("navigation", { name: "Mobile navigation" });
    const last = within(menu).getByRole("link", { name: "Source on GitHub" });
    const sentinel = screen.getByRole("button", { name: "After navigation" });
    last.focus();
    const tab = createEvent.keyDown(last, { key: "Tab", bubbles: true, cancelable: true });
    fireEvent(last, tab);
    if (!tab.defaultPrevented) sentinel.focus();

    expect(tab.defaultPrevented).toBe(false);
    expect(sentinel).toHaveFocus();
  });

  it("closes an already-active route and restores the menu trigger", () => {
    render(<TopBar {...props} />);
    const trigger = screen.getByRole("button", { name: "Open menu" });
    fireEvent.click(trigger);

    fireEvent.click(within(screen.getByRole("navigation", { name: "Mobile navigation" })).getByRole("link", { name: "Overview" }));

    expect(screen.queryByRole("navigation", { name: "Mobile navigation" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("does not restore trigger focus when selecting a different route", () => {
    render(<TopBar {...props} />);
    const trigger = screen.getByRole("button", { name: "Open menu" });
    fireEvent.click(trigger);

    fireEvent.click(within(screen.getByRole("navigation", { name: "Mobile navigation" })).getByRole("link", { name: "Methodology" }));

    expect(screen.queryByRole("navigation", { name: "Mobile navigation" })).not.toBeInTheDocument();
    expect(trigger).not.toHaveFocus();
  });
});
