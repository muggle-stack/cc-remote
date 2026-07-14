import { createRoot } from "react-dom/client";
import "./index.css";
import "./App.css";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import { useMobileViewport } from "./use-mobile-viewport";

export function RootApp() {
  useMobileViewport();
  return <App />;
}

createRoot(document.getElementById("root")!).render(
  <ErrorBoundary>
    <RootApp />
  </ErrorBoundary>
);
