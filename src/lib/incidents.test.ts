import { describe, expect, it } from "vitest";
import { trailingNoEntryRun } from "./incidents";

const day = (date: string, opened: number) => ({ date, cumulative_realized: 0, opened });

describe("trailingNoEntryRun", () => {
  const week = [day("2026-11-05", 4), day("2026-11-06", 0), day("2026-11-07", 0)];

  it("counts a finished empty day", () => {
    expect(trailingNoEntryRun(week)).toEqual({ start: "2026-11-06", end: "2026-11-07", count: 2 });
  });

  it("does not count the day still in progress", () => {
    expect(trailingNoEntryRun(week, "2026-11-07")).toEqual({ start: "2026-11-06", end: "2026-11-06", count: 1 });
  });

  it("reports no run when only today is empty", () => {
    expect(trailingNoEntryRun([day("2026-11-06", 3), day("2026-11-07", 0)], "2026-11-07")).toBeNull();
  });

  it("reports no run when the series publishes no entry counts", () => {
    expect(trailingNoEntryRun([{ date: "2026-11-06", cumulative_realized: 0 }])).toBeNull();
  });
});
