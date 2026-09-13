import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SystemHighlights } from "./SystemHighlights";

/** Minutes between runs implied by a timer unit's OnCalendar minute list. */
function cadenceMinutes(unit: string): number {
  const line = readFileSync(resolve(process.cwd(), `trading/deploy/aws/systemd/${unit}`), "utf8")
    .split("\n")
    .find((raw) => raw.startsWith("OnCalendar="));
  const minutes = line?.split(":").pop()?.split(",") ?? [];
  expect(minutes.length).toBeGreaterThan(1);
  return 60 / minutes.length;
}

describe("production-discipline cadence copy", () => {
  // The scan timer and the publish timer are two different units on two
  // different cadences. A sentence that carries one number attaches it to
  // whichever verb it sits beside and states the other one wrongly, which is
  // how "every 5 minutes" and then "every 10 minutes" were each false in turn.
  it("gives the scan and the publish their own cadence, each matching its own timer unit", () => {
    const scan = cadenceMinutes("sfo-kalshi-paper-scan.timer");
    const publish = cadenceMinutes("sfo-operational-publish.timer");
    expect(scan).not.toBe(publish);

    render(<SystemHighlights />);

    expect(
      screen.getByText(
        `Unattended AWS timers scan every city's markets every ${scan} minutes and publish the runtime artifacts every ${publish} minutes`,
      ),
    ).toBeInTheDocument();
  });
});
