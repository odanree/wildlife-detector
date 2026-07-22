import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AlertsPage } from "./pages/AlertsPage";
import { BaselinesPage } from "./pages/BaselinesPage";
import { LivePreviewPage } from "./pages/LivePreviewPage";
import { StatusDashboard } from "./pages/StatusDashboard";

/**
 * Router shell. `basename="/react"` matches the Vite build's `base:
 * "/react/"` config and the Flask serving prefix — nested Links use
 * root-relative paths within that prefix. Unknown paths fall back to
 * /preview.
 *
 * Post-strangler-fig cutover (PR #16): the old vanilla-JS index at /
 * is gone; the Flask sidecar 302s / → /react/preview. The former
 * LandingPage splash at /react/ is also retired — that page was a
 * mid-migration nav aid pointing at both React and vanilla-JS routes,
 * useless now that vanilla is deleted. `/react/` and any Link to="/"
 * within the app now redirect to /preview (the main working page).
 */
export function App() {
  return (
    <BrowserRouter basename="/react">
      <Routes>
        <Route path="/" element={<Navigate to="/preview" replace />} />
        <Route path="/preview" element={<LivePreviewPage />} />
        <Route path="/status" element={<StatusDashboard />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/baselines" element={<BaselinesPage />} />
        <Route path="*" element={<Navigate to="/preview" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
