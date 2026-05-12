import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { ProtectedRoute } from "@/auth/ProtectedRoute";
import { EasterEggs } from "@/components/EasterEggs";
import { Layout } from "@/components/Layout";
import { AdminGrantsPage } from "@/pages/AdminGrants";
import { AdminUsersPage } from "@/pages/AdminUsers";
import { CapabilitiesPage } from "@/pages/Capabilities";
import { ChatPage } from "@/pages/Chat";
import { DashboardPage } from "@/pages/Dashboard";
import { LiveProcessesPage } from "@/pages/LiveProcesses";
import { LoginPage } from "@/pages/Login";
import { RunDetailPage } from "@/pages/RunDetail";
import { RunsPage } from "@/pages/Runs";
import { SuperuserTenantDetailPage } from "@/pages/SuperuserTenantDetail";
import { SuperuserTenantsPage } from "@/pages/SuperuserTenants";
import { SuperuserUsersPage } from "@/pages/SuperuserUsers";
import { WorkflowDetailPage } from "@/pages/WorkflowDetail";
import { WorkflowsPage } from "@/pages/Workflows";

export default function App() {
  return (
    <>
      <EasterEggs />
      <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route index element={<HomeRedirect />} />

        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="chat/:id" element={<ChatPage />} />
        <Route path="workflows" element={<WorkflowsPage />} />
        <Route path="workflows/:id" element={<WorkflowDetailPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="runs/:id" element={<RunDetailPage />} />
        <Route path="capabilities" element={<CapabilitiesPage />} />

        <Route
          path="live"
          element={
            <ProtectedRoute requireRole={["tenant_admin", "superuser"]}>
              <LiveProcessesPage />
            </ProtectedRoute>
          }
        />

        <Route
          path="admin/users"
          element={
            <ProtectedRoute requireRole={["tenant_admin"]}>
              <AdminUsersPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="admin/grants"
          element={
            <ProtectedRoute requireRole={["tenant_admin"]}>
              <AdminGrantsPage />
            </ProtectedRoute>
          }
        />

        <Route
          path="superuser/tenants"
          element={
            <ProtectedRoute requireRole={["superuser"]}>
              <SuperuserTenantsPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="superuser/tenants/:id"
          element={
            <ProtectedRoute requireRole={["superuser"]}>
              <SuperuserTenantDetailPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="superuser/users"
          element={
            <ProtectedRoute requireRole={["superuser"]}>
              <SuperuserUsersPage />
            </ProtectedRoute>
          }
        />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  );
}

function HomeRedirect() {
  const { claims } = useAuth();
  if (!claims) return <Navigate to="/login" replace />;
  return <Navigate to="/dashboard" replace />;
}
