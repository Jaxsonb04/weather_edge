import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import { StrategyPublicationNotice } from "./StrategyPublicationNotice";

const publication = (generatedAt: string | null, status = "ready"): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "strategy_research.json": { generated_at: generatedAt, sha256: generatedAt ? "strategy" : null, status },
  },
});

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("StrategyPublicationNotice", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    fetchMock.mockReset();
  });

  async function renderNotice(payload: PublicationManifest, generatedAt?: string) {
    fetchMock.mockResolvedValue(ok(payload));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["strategy_research.json"]} />
        <StrategyPublicationNotice generatedAt={generatedAt} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("shows stale Strategy Lab recovery copy and its generated time", async () => {
    await renderNotice(publication("2026-07-09T10:00:00Z"));

    expect(screen.getByRole("alert")).toHaveTextContent(/Strategy Lab data is behind/i);
    expect(screen.getByRole("alert")).toHaveTextContent(/open positions, pending limits, and current candidate counts aren't shown/i);
    expect(screen.getByRole("alert").querySelector("time")).toHaveAttribute("datetime", "2026-07-09T10:00:00Z");
  });

  it("shows unknown freshness explicitly with the artifact generated time", async () => {
    await renderNotice(publication(null, "missing"), "2026-07-09T11:30:00Z");

    expect(screen.getByRole("status")).toHaveTextContent(/Live Strategy Lab status unavailable/i);
    expect(screen.getByRole("status").querySelector("time")).toHaveAttribute("datetime", "2026-07-09T11:30:00Z");
  });

  it("is hidden while Strategy Lab publication is fresh", async () => {
    await renderNotice(publication("2026-07-09T11:59:00Z"));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
