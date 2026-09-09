import { useState } from "react";
import { createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

function MenuHarness() {
  const [open, setOpen] = useState(false);
  return <TopBar {...props} menuOpen={open} onMenuOpenChange={setOpen} />;
}

describe("TopBar mobile menu keyboard behavior", () => {
  it("keeps brand and source link names explicit when responsive text is hidden", () => {
    render(<MenuHarness />);

    const brandLink = screen.getByRole("link", { name: "WeatherEdge overview" });
    expect(brandLink).toHaveAttribute(
      "aria-label",
      "WeatherEdge overview",
    );
    expect(brandLink.querySelector("img")).toHaveAttribute("src", "/favicon.svg");
    expect(brandLink.querySelector("img")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("link", { name: "WeatherEdge source on GitHub" })).toHaveAttribute(
      "aria-label",
      "WeatherEdge source on GitHub",
    );
  });

  it("opens a named modal and restores the trigger on Escape", async () => {
    render(<MenuHarness />);
    const trigger = screen.getByRole("button", { name: "Open menu" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Navigation" });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("isolates the menu from background controls and offers a named close button", async () => {
    render(
      <>
        <MenuHarness />
        <button type="button">After navigation</button>
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));

    const dialog = await screen.findByRole("dialog", { name: "Navigation" });
    expect(screen.queryByRole("button", { name: "After navigation" })).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Close menu" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "After navigation" })).toBeInTheDocument();
  });

  it("closes an already-active route and restores the menu trigger", async () => {
    render(<MenuHarness />);
    const trigger = screen.getByRole("button", { name: "Open menu" });
    trigger.focus();
    fireEvent.click(trigger);

    fireEvent.click(within(screen.getByRole("navigation", { name: "Mobile navigation" })).getByRole("link", { name: "Overview" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("preserves native navigation when selecting a different route", async () => {
    render(<MenuHarness />);
    const trigger = screen.getByRole("button", { name: "Open menu" });
    fireEvent.click(trigger);

    const link = within(screen.getByRole("navigation", { name: "Mobile navigation" })).getByRole("link", { name: "Methodology" });
    expect(link).toHaveAttribute("href", "#/methodology");
    const click = createEvent.click(link, { bubbles: true, cancelable: true });
    fireEvent(link, click);

    expect(click.defaultPrevented).toBe(false);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
