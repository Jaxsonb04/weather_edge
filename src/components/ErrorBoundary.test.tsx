import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

function BrokenView(): never {
  throw new Error("view exploded");
}

describe("ErrorBoundary", () => {
  it("localizes a view render failure in the existing error state", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <ErrorBoundary>
        <BrokenView />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load the forecast");
    expect(screen.getByRole("alert")).toHaveTextContent("view exploded");
    consoleError.mockRestore();
  });

  it("identifies a Strategy Lab render failure and preserves the reload action", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<ErrorBoundary title="Couldn't load Strategy Lab"><BrokenView /></ErrorBoundary>);

    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load Strategy Lab");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Couldn't load the forecast");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    consoleError.mockRestore();
  });
});
