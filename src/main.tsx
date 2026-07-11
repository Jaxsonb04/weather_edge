import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { PublicationProvider } from "./lib/publication";
import "./lib/register-icons";

// Note: intentionally not wrapped in <StrictMode>. React 19's StrictMode
// double-invokes mount effects in dev, which prevents motion/react entrance
// animations (animate + whileInView) from settling. Production is unaffected.
createRoot(document.getElementById("root")!).render(
  <PublicationProvider>
    <App />
  </PublicationProvider>,
);
