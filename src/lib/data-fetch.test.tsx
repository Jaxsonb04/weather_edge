import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "./publication";
import { useResource } from "./data";

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

const published = (snapshot: string, hash: string): PublicationManifest => ({
  snapshot_id: snapshot,
  published_at: "2026-07-09T12:00:00Z",
  artifacts: {
    "example.json": {
      generated_at: "2026-07-09T12:00:00Z",
      sha256: hash,
      status: "ready",
    },
  },
});

function ResourceProbe() {
  const { data } = useResource<{ value: string }>("example.json");
  return <p>{data?.value ?? "loading"}</p>;
}

describe("useResource publication versioning", () => {
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

  it("refetches on an artifact hash change and keeps the last good data while refreshing", async () => {
    let manifestCall = 0;
    let resolveSecond!: (response: Response) => void;
    const secondResource = new Promise<Response>((resolve) => {
      resolveSecond = resolve;
    });
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("publication_manifest.json")) {
        manifestCall += 1;
        return ok(
          manifestCall === 1
            ? published("snapshot-one", "hash-one")
            : published("snapshot-two", "hash-two"),
        );
      }
      if (url.includes("example.json?v=hash-two")) return secondResource;
      return ok({ value: "first snapshot" });
    });

    render(
      <PublicationProvider>
        <ResourceProbe />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("first snapshot")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(60_000));

    expect(screen.getByText("first snapshot")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("example.json?v=hash-two"),
      expect.objectContaining({ cache: "no-store" }),
    );

    resolveSecond(ok({ value: "second snapshot" }));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("second snapshot")).toBeInTheDocument();
  });
});
