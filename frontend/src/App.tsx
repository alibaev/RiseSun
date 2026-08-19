import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { MetersListPage } from "./pages/MetersListPage";
import { MeterDetailPage } from "./pages/MeterDetailPage";
import { ParameterSchemesPage } from "./pages/ParameterSchemesPage";
import { ScheduledJobsPage } from "./pages/ScheduledJobsPage";

export function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<Layout />}>
            <Route path="/meters" element={<MetersListPage />} />
            <Route path="/meters/:id" element={<MeterDetailPage />} />
            <Route path="/schemes" element={<ParameterSchemesPage />} />
            <Route path="/scheduled-jobs" element={<ScheduledJobsPage />} />
            <Route path="/" element={<Navigate to="/meters" replace />} />
          </Route>
        </Route>
      </Routes>
    </AuthProvider>
  );
}
