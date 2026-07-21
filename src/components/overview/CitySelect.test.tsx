import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { City } from "../../lib/data";
import { CitySelect } from "./CitySelect";

const cities = [
  { slug: "sfo", name: "San Francisco", series_ticker: "KXHIGHTSFO" },
  { slug: "mia", name: "Miami", series_ticker: "KXHIGHMIA" },
] as City[];

describe("CitySelect", () => {
  it("reports a newly selected city", async () => {
    const onSelect = vi.fn();
    render(<CitySelect cities={cities} selected="sfo" onSelect={onSelect} />);

    fireEvent.click(screen.getByRole("button", { name: /San Francisco/i }));
    fireEvent.click(await screen.findByRole("option", { name: "Miami" }));

    expect(onSelect).toHaveBeenCalledWith("mia");
  });
});
