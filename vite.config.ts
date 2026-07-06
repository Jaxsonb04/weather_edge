import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Published to GitHub Pages — relative base so built asset URLs resolve under
// any subpath (e.g. /weather_edge/).
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  // Ensure a single React instance across app code + motion + number-flow +
  // HeroUI, otherwise dev pre-bundling can yield "Invalid hook call".
  resolve: {
    dedupe: ["react", "react-dom"],
  },
  optimizeDeps: {
    include: [
      "react",
      "react-dom",
      "react-dom/client",
      "react/jsx-runtime",
      "motion",
      "motion/react",
      "@number-flow/react",
      "@iconify/react",
    ],
  },
});
