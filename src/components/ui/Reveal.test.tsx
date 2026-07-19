import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Reveal } from "./Reveal";

describe("Reveal", () => {
  it("renders immediate content visibly without waiting for an animation frame", () => {
    render(
      <Reveal immediate>
        <span>Forecasting</span>
      </Reveal>,
    );

    expect(screen.getByText("Forecasting").parentElement).toHaveClass("is-in");
  });
});
