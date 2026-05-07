import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";

export function ProtectedRoute({
  children,
  requireRole,
}: {
  children: ReactNode;
  requireRole?: ("superuser" | "tenant_admin" | "tenant_user")[];
}) {
  const { claims } = useAuth();
  const location = useLocation();

  if (!claims) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  if (requireRole && !requireRole.includes(claims.role)) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}
