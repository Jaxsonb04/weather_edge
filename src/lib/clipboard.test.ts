import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "./clipboard";

describe("copyText", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns true only after the clipboard write resolves", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await expect(copyText("KXHIGHTSFO")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("KXHIGHTSFO");
  });

  it("returns false when clipboard access rejects or is unavailable", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    await expect(copyText("KXHIGHTSFO")).resolves.toBe(false);

    vi.stubGlobal("navigator", {});
    await expect(copyText("KXHIGHTSFO")).resolves.toBe(false);
  });
});
