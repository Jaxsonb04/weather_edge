import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PublicationProvider,
  usePublication,
  usePublicationClock,
  type PublicationManifest,
} from "./publication";
import { PublicationLoaded } from "../test/PublicationLoaded";

const manifest = (generatedAt: string): PublicationManifest => ({
  schema_version: 1,
  snapshot_id: "0123456789abcdef01234567",
  published_at: generatedAt,
  artifacts: {
    "trading_signal.json": { generated_at: generatedAt, sha256: "signal-hash", status: "ready" },
    "cities_data.json": { generated_at: generatedAt, sha256: "cities-hash", status: "ready" },
    "strategy_research.json": { generated_at: generatedAt, sha256: "strategy-hash", status: "ready" },
  },
});

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

function Probe() {
  const publication = usePublication();
  return (
    <dl>
      <dt>Operational</dt>
      <dd>{publication.operational.state}</dd>
      <dt>Strategy</dt>
      <dd>{publication.strategy.state}</dd>
      <dt>Snapshot</dt>
      <dd>{publication.snapshotVersion ?? "none"}</dd>
      <dt>Signal hash</dt>
      <dd>{publication.artifactHashes["trading_signal.json"] ?? "none"}</dd>
    </dl>
  );
}

describe("PublicationProvider", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    fetchMock.mockReset();
  });

  it("marks a July 7 publication stale when viewed on July 9", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    fetchMock.mockResolvedValue(ok(manifest("2026-07-07T12:00:00Z")));

    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json", "strategy_research.json"]} />
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    expect(screen.getByText("Operational").nextElementSibling).toHaveTextContent("stale");
    expect(screen.getByText("Strategy").nextElementSibling).toHaveTextContent("stale");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("publication_manifest.json?poll="),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("uses coherent publication time rather than an older artifact generation time", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    const payload = manifest("2026-07-09T11:59:00Z");
    payload.artifacts!["trading_signal.json"]!.generated_at = "2026-07-09T11:35:00Z";
    payload.artifacts!["cities_data.json"]!.generated_at = "2026-07-09T11:35:00Z";
    fetchMock.mockResolvedValue(ok(payload));

    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    expect(screen.getByText("Operational").nextElementSibling).toHaveTextContent("fresh");
  });

  it("treats missing optional manifest fields as unknown without crashing", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    fetchMock.mockResolvedValue(ok({ artifacts: { "trading_signal.json": {} } }));

    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json", "strategy_research.json"]} />
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    expect(screen.getByText("Operational").nextElementSibling).toHaveTextContent("unknown");
    expect(screen.getByText("Strategy").nextElementSibling).toHaveTextContent("unknown");
    expect(screen.getByText("Snapshot").nextElementSibling).toHaveTextContent("none");
  });

  it("keeps preserved Strategy data stale when operational data is republished", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    const payload = manifest("2026-07-09T11:59:00Z");
    payload.artifacts!["strategy_research.json"] = {
      generated_at: "2026-07-09T10:00:00Z", sha256: "strategy-hash", status: "preserved",
    };
    fetchMock.mockResolvedValue(ok(payload));

    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json", "strategy_research.json"]} />
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    expect(screen.getByText("Operational").nextElementSibling).toHaveTextContent("fresh");
    expect(screen.getByText("Strategy").nextElementSibling).toHaveTextContent("stale");
  });

  it("expires Strategy data on its own deadline even while publication remains fresh", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    const payload = manifest("2026-07-09T11:59:00Z");
    payload.artifacts!["strategy_research.json"]!.generated_at = "2026-07-09T11:41:00Z";
    fetchMock.mockResolvedValue(ok(payload));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json", "strategy_research.json"]} />
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("Strategy").nextElementSibling).toHaveTextContent("fresh");
    await act(async () => vi.advanceTimersByTimeAsync(60_001));
    expect(screen.getByText("Strategy").nextElementSibling).toHaveTextContent("stale");
    expect(screen.getByText("Operational").nextElementSibling).toHaveTextContent("fresh");
  });

  it("recomputes freshness as time advances", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    fetchMock.mockResolvedValue(ok(manifest("2026-07-09T11:55:00Z")));

    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json", "strategy_research.json"]} />
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("Operational").nextElementSibling).toHaveTextContent("fresh");

    // age 17m: past the 15-minute operational threshold, inside strategy 20.
    await act(async () => vi.advanceTimersByTimeAsync(12 * 60_000));

    expect(screen.getByText("Operational").nextElementSibling).toHaveTextContent("stale");
    expect(screen.getByText("Strategy").nextElementSibling).toHaveTextContent("fresh");
  });

  it("retains the last valid manifest when a later poll fails", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    fetchMock
      .mockResolvedValueOnce(ok(manifest("2026-07-09T11:59:00Z")))
      .mockRejectedValueOnce(new Error("temporary network failure"));

    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json", "strategy_research.json"]} />
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("Signal hash").nextElementSibling).toHaveTextContent("signal-hash");

    await act(async () => vi.advanceTimersByTimeAsync(60_000));

    expect(screen.getByText("Signal hash").nextElementSibling).toHaveTextContent("signal-hash");
  });

  it("waits for each manifest request to finish before scheduling the next poll", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    let resolveFirst!: (response: Response) => void;
    fetchMock.mockImplementationOnce(
      () => new Promise<Response>((resolve) => { resolveFirst = resolve; }),
    );

    render(
      <PublicationProvider>
        <Probe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(120_000));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveFirst(ok(manifest("2026-07-09T11:59:00Z")));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    fetchMock.mockResolvedValue(ok(manifest("2026-07-09T12:00:00Z")));
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const firstUrl = String(fetchMock.mock.calls[0]?.[0]);
    const secondUrl = String(fetchMock.mock.calls[1]?.[0]);
    expect(firstUrl).toContain("publication_manifest.json?poll=");
    expect(secondUrl).not.toBe(firstUrl);
  });

  it("keeps ordinary consumers still on an age tick while the status banner rerenders", async () => {
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    fetchMock.mockResolvedValue(ok(manifest("2026-07-09T10:00:00Z")));
    let consumerRenders = 0;
    let bannerRenders = 0;
    const { PublicationStatusBanner } = await import("../components/layout/PublicationStatusBanner");
    function CountedConsumer() {
      const publication = usePublication();
      consumerRenders += 1;
      return <p>snapshot: {publication.snapshotVersion}</p>;
    }
    function CountedBanner() {
      usePublicationClock();
      bannerRenders += 1;
      return <PublicationStatusBanner />;
    }

    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <CountedConsumer />
        <CountedBanner />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    consumerRenders = 0;
    bannerRenders = 0;

    await act(async () => vi.advanceTimersByTimeAsync(60_000));

    expect(screen.getByRole("alert")).toHaveTextContent(/2h 1m ago/i);
    expect(consumerRenders).toBe(0);
    expect(bannerRenders).toBeGreaterThan(0);
  });
});
