import { createRoot } from "react-dom/client";
import { Toast } from "@heroui/react";
import "./index.css";
import App from "./App.tsx";

// Note: intentionally not wrapped in <StrictMode>. React 19's StrictMode
// double-invokes mount effects in dev, which prevents motion/react entrance
// animations (animate + whileInView) from settling. Production is unaffected.
createRoot(document.getElementById("root")!).render(
  <>
    {/* The only "provider" in HeroUI v3: a portalled toast region, mounted once. */}
    <Toast.Provider placement="bottom end" />
    <App />
  </>,
);
