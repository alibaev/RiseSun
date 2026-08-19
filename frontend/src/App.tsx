import { Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { DashboardPage } from "./pages/DashboardPage";
import { MetersListPage } from "./pages/MetersListPage";
import { MeterDetailPage } from "./pages/MeterDetailPage";
import { ParameterSchemesPage } from "./pages/ParameterSchemesPage";
import { ScheduledJobsPage } from "./pages/ScheduledJobsPage";
import { AuditLogPage } from "./pages/AuditLogPage";
import { UsersPage } from "./pages/UsersPage";
import { BillingKeysPage } from "./pages/BillingKeysPage";

export function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<Layout />}>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/meters" element={<MetersListPage />} />
            <Route path="/meters/:id" element={<MeterDetailPage />} />
            <Route path="/schemes" element={<ParameterSchemesPage />} />
            <Route path="/scheduled-jobs" element={<ScheduledJobsPage />} />
            <Route path="/audit-log" element={<AuditLogPage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/billing-keys" element={<BillingKeysPage />} />
          </Route>
        </Route>
      </Routes>
    </AuthProvider>
  );
}
