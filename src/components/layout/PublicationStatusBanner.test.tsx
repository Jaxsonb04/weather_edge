import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationStatusBanner } from "./PublicationStatusBanner";
import { PublicationLoaded } from "../../test/PublicationLoaded";

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

const manifest = (generatedAt: string): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  published_at: generatedAt,
  artifacts: {
    "trading_signal.json": { generated_at: generatedAt, sha256: "signal", status: "ready" },
    "cities_data.json": { generated_at: generatedAt, sha256: "cities", status: "ready" },
  },
});

describe("PublicationStatusBanner", () => {
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

  async function renderBanner(payload: unknown) {
    fetchMock.mockResolvedValue(ok(payload));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <PublicationStatusBanner />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("shows a stale alert with clear suppression and recovery copy", async () => {
    await renderBanner(manifest("2026-07-07T12:00:00Z"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      /real-time prediction-market and open-position data is paused until the feed catches up/i,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/last operational publication/i);
  });

  it("shows a quieter but explicit status when the manifest is unavailable", async () => {
    fetchMock.mockRejectedValue(new Error("not deployed"));
    render(
      <PublicationProvider>
        <PublicationStatusBanner />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    expect(screen.getByRole("status")).toHaveTextContent(/live status unavailable/i);
    expect(screen.getByRole("status")).toHaveTextContent(/isn't being shown for this deployment/i);
  });

  it("stays out of the way while publication is fresh", async () => {
    await renderBanner(manifest("2026-07-09T11:59:00Z"));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
