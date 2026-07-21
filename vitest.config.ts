import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: {
    dedupe: ["react", "react-dom"],
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    // Only the SPA source is under test. Without this, vitest walks the whole
    // project root and picks up stale copies of these same specs living in
    // gitignored scratch directories (.local staging areas, git worktrees),
    // which fail against their own frozen fixtures and mask the real result.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["**/node_modules/**", "**/dist/**", ".local/**", ".worktrees/**"],
  },
});
