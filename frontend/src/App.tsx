import { Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { DashboardPage } from "./pages/DashboardPage";
import { MetersListPage } from "./pages/MetersListPage";
import { ResPage } from "./pages/ResPage";
import { MeterDetailPage } from "./pages/MeterDetailPage";
import { ObisCatalogPage } from "./pages/ObisCatalogPage";
import { ParameterSchemesPage } from "./pages/ParameterSchemesPage";
import { PollProfilesPage } from "./pages/PollProfilesPage";
import { ScheduledJobsPage } from "./pages/ScheduledJobsPage";
import { AuditLogPage } from "./pages/AuditLogPage";
import { UsersPage } from "./pages/UsersPage";
import { BillingKeysPage } from "./pages/BillingKeysPage";
import { GatewaysPage } from "./pages/GatewaysPage";
import { ComingSoonPage } from "./pages/ComingSoonPage";

export function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<Layout />}>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/meters" element={<MetersListPage />} />
            <Route path="/res" element={<ResPage />} />
            <Route path="/meters/:id" element={<MeterDetailPage />} />
            <Route path="/schemes" element={<ParameterSchemesPage />} />
            <Route path="/obis-catalog" element={<ObisCatalogPage />} />
            <Route path="/poll-profiles" element={<PollProfilesPage />} />
            <Route path="/scheduled-jobs" element={<ScheduledJobsPage />} />
            <Route path="/audit-log" element={<AuditLogPage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/billing-keys" element={<BillingKeysPage />} />
            <Route path="/gateways" element={<GatewaysPage />} />
            <Route path="/soon" element={<ComingSoonPage />} />
          </Route>
        </Route>
      </Routes>
    </AuthProvider>
  );
}
