import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { auth as authApi } from "@/api";
import { configureAuth } from "@/api/client";

const TOKEN_KEY = "aakar.token";
const CLAIMS_KEY = "aakar.claims";

export interface SessionClaims {
  user_id: string;
  tenant_id: string | null;
  role: "superuser" | "tenant_admin" | "tenant_user";
  expires_at: number; // epoch seconds
  email: string; // remembered from login form for the header
}

interface AuthState {
  token: string | null;
  claims: SessionClaims | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const [, payload] = token.split(".");
    if (!payload) return null;
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function readPersisted(): { token: string | null; claims: SessionClaims | null } {
  const token = localStorage.getItem(TOKEN_KEY);
  const claimsRaw = localStorage.getItem(CLAIMS_KEY);
  if (!token || !claimsRaw) return { token: null, claims: null };
  try {
    const claims = JSON.parse(claimsRaw) as SessionClaims;
    if (claims.expires_at * 1000 < Date.now()) return { token: null, claims: null };
    return { token, claims };
  } catch {
    return { token: null, claims: null };
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const initial = readPersisted();
  const [token, setToken] = useState<string | null>(initial.token);
  const [claims, setClaims] = useState<SessionClaims | null>(initial.claims);
  const [loading, setLoading] = useState(false);

  const logout = useCallback(() => {
    setToken(null);
    setClaims(null);
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(CLAIMS_KEY);
  }, []);

  // Wire the API client to read this state.
  useEffect(() => {
    configureAuth({
      getToken: () => token,
      onUnauthorized: () => logout(),
    });
  }, [token, logout]);

  const login = useCallback(
    async (email: string, password: string) => {
      setLoading(true);
      try {
        const response = await authApi.login(email, password);
        const payload = decodeJwtPayload(response.access_token);
        if (!payload) throw new Error("invalid token returned by server");
        const sessionClaims: SessionClaims = {
          user_id: String(payload.sub ?? ""),
          tenant_id:
            payload.tid && payload.tid !== "superuser" ? String(payload.tid) : null,
          role: (payload.role as SessionClaims["role"]) ?? "tenant_user",
          expires_at: Number(payload.exp ?? 0),
          email,
        };
        localStorage.setItem(TOKEN_KEY, response.access_token);
        localStorage.setItem(CLAIMS_KEY, JSON.stringify(sessionClaims));
        setToken(response.access_token);
        setClaims(sessionClaims);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const value = useMemo<AuthState>(
    () => ({ token, claims, loading, login, logout }),
    [token, claims, loading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
