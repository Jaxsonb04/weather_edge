import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

describe("selective component CSS loading", () => {
  it("loads Segment eagerly while retaining below-fold overview dependencies in its chunk", () => {
    expect(read("src/index.css")).toContain('@heroui-pro/react/css/components/segment.css');
    expect(read("src/styles/pro-overview.css")).not.toContain('@heroui-pro/react/css/components/segment.css');
    expect(read("src/styles/pro-overview.css")).toContain('@heroui/styles/components/list-box-item.css');
    expect(read("src/styles/pro-overview.css")).toContain('@heroui/styles/components/progress-bar.css');
  });

  it("co-locates table, checkbox, and close-button styles with their route chunks", () => {
    for (const path of ["src/styles/pro-city-detail.css", "src/styles/pro-strategy.css"]) {
      expect(read(path)).toContain('@heroui/styles/components/table.css');
      expect(read(path)).toContain('@heroui/styles/components/checkbox.css');
    }
    expect(read("src/styles/pro-city-detail.css")).toContain('@heroui/styles/components/close-button.css');
    expect(read("src/styles/pro-command.css")).toContain('@heroui/styles/components/close-button.css');
  });
});
