import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AlertsPage } from "./pages/AlertsPage";
import { LandingPage } from "./pages/LandingPage";

/**
 * Router shell. `basename` matches the Vite `base: "/react/"` config and
 * the Flask serving prefix — nested Links use root-relative paths within
 * that prefix. Unknown paths fall back to the landing page so the SPA
 * catch-all in Flask (/react/<path:_p>) doesn't strand users.
 *
 * Pattern: prefixed base path as URL contract. The "/react/" prefix IS
 * the version boundary — old vanilla-JS routes at /, /alerts, /baselines
 * run untouched in parallel until each is migrated (strangler-fig).
 */
export function App() {
  return (
    <BrowserRouter basename="/react">
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
