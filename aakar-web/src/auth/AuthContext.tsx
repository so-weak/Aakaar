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
  tenant_slug: string | null;
  tenant_name: string | null;
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
  const token = sessionStorage.getItem(TOKEN_KEY);
  const claimsRaw = sessionStorage.getItem(CLAIMS_KEY);
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
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(CLAIMS_KEY);
  }, []);

  // Wire the API client to read the token. We read straight from
  // sessionStorage rather than closing over the React `token` state — the
  // `setToken` setter is async (writes apply on the next render), and
  // login → navigate → first authenticated query can fire before the
  // closure refreshes, producing a "missing bearer token" 401. Reading
  // sessionStorage is synchronous and `login()` writes to it before
  // setToken, so the next request always sees the fresh value.
  //
  // We use sessionStorage (not localStorage) so each browser tab holds
  // its own session — opening Aakar in two tabs lets the operator log
  // in as different users (e.g. tenant_admin in one, super in another)
  // without one tab clobbering the other.
  useEffect(() => {
    configureAuth({
      getToken: () => sessionStorage.getItem(TOKEN_KEY),
      onUnauthorized: () => logout(),
    });
  }, [logout]);

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
          tenant_slug: response.tenant_slug ?? null,
          tenant_name: response.tenant_name ?? null,
        };
        sessionStorage.setItem(TOKEN_KEY, response.access_token);
        sessionStorage.setItem(CLAIMS_KEY, JSON.stringify(sessionClaims));
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
