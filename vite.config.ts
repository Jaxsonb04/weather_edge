import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

function fontPreloads(): Plugin {
  return {
    name: "weatheredge-font-preloads",
    enforce: "post" as const,
    generateBundle(_options, bundle) {
      const html = Object.values(bundle).find(
        (entry) => entry.type === "asset" && entry.fileName === "index.html",
      );
      if (!html || html.type !== "asset" || typeof html.source !== "string") return;
      const fonts = Object.values(bundle)
        .filter(
          (entry) => entry.type === "asset" && entry.fileName.endsWith(".woff2"),
        )
        .map((entry) => entry.fileName)
        .sort();
      const links = fonts
        .map((fileName) => `    <link rel="preload" href="./${fileName}" as="font" type="font/woff2" crossorigin />`)
        .join("\n");
      let source = html.source.replace("  </head>", `${links}\n  </head>`);

      // Vite normally emits the module script before the generated stylesheet.
      // A fast script response can therefore mount the Tailwind layout before
      // its CSS has arrived, briefly placing the footer in the first viewport
      // and producing a large layout shift. Put render-blocking styles first so
      // the initial React tree is measured with its final layout primitives.
      const stylesheetPattern = /^\s*<link rel="stylesheet"[^>]*>\s*$/gm;
      const stylesheets = source.match(stylesheetPattern) ?? [];
      if (stylesheets.length) {
        source = source.replace(stylesheetPattern, "");
        source = source.replace(
          /^(\s*)<script type="module"/m,
          `${stylesheets.map((link) => `    ${link.trim()}`).join("\n")}\n$1<script type="module"`,
        );
      }
      html.source = source;
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
