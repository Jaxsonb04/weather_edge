import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import type { OutputAsset } from "rolldown";

function fontPreloads(): Plugin {
  return {
    name: "weatheredge-font-preloads",
    enforce: "post" as const,
    generateBundle(_options, bundle) {
      const html = Object.values(bundle).find(
        (entry): entry is OutputAsset => entry.type === "asset" && entry.fileName === "index.html",
      );
      if (!html || typeof html.source !== "string") return;
      const fonts = Object.values(bundle)
        .filter(
          (entry): entry is OutputAsset => entry.type === "asset" && entry.fileName.endsWith(".woff2"),
        )
        .map((entry) => entry.fileName)
        .sort();
      const links = fonts
        .map((fileName) => `    <link rel="preload" href="./${fileName}" as="font" type="font/woff2" crossorigin />`)
        .join("\n");
      html.source = html.source.replace("  </head>", `${links}\n  </head>`);
    },
  };
}

// Published to GitHub Pages — relative base so built asset URLs resolve under
// any subpath (e.g. /weather_edge/).
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss(), fontPreloads()],
  build: {
    cssMinify: "lightningcss",
    manifest: true,
  },
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
      "@iconify/react/offline",
    ],
  },
});
