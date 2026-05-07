import { useState } from "react";
import type { FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { MorphLogo } from "@/components/MorphLogo";

export function LoginPage() {
  const { login, claims, loading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (claims) return <Navigate to={from} replace />;

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await login(email, password);
      navigate(from, { replace: true });
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : (err as Error).message;
      setError(detail || "Login failed");
    }
  };

  return (
    <div className="noise-shell grid h-full place-items-center overflow-hidden bg-ink-950 px-4">
      <div className="pointer-events-none absolute left-10 top-8 hidden rotate-[-8deg] border border-signal-pink px-4 py-2 font-mono text-xs uppercase tracking-[0.3em] text-signal-pink md:block">
        plan loud
      </div>
      <div className="pointer-events-none absolute bottom-8 right-10 hidden rotate-3 border border-signal-cyan px-4 py-2 font-mono text-xs uppercase tracking-[0.3em] text-signal-cyan md:block">
        run clean
      </div>

      <div className="w-full max-w-md">
        <div className="mb-8">
          <div className="mb-5 flex items-center justify-center">
            <span
              className="grid h-16 w-16 place-items-center rounded-md border border-accent-300 bg-accent-300 text-ink-950 shadow-[8px_8px_0_rgb(255_59_147)]"
              aria-hidden="true"
            >
              <MorphLogo />
            </span>
          </div>
          <div className="text-center">
            <div className="text-4xl font-black uppercase tracking-[0.22em] text-ink-50">
              Aakar
            </div>
            <div className="mt-2 font-mono text-[11px] uppercase tracking-[0.28em] text-accent-200">
              workflow platform
            </div>
          </div>
        </div>

        <form onSubmit={onSubmit} className="card space-y-4 p-6">
          <div>
            <span className="stamp">secure desk</span>
            <h1 className="mt-3 text-xl font-black uppercase tracking-wide text-ink-50">
              Sign in
            </h1>
          </div>

          <label className="block">
            <span className="panel-title">Email</span>
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="input mt-1"
              placeholder="you@company.test"
            />
          </label>

          <label className="block">
            <span className="panel-title">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="input mt-1"
            />
          </label>

          {error ? (
            <div className="rounded-md border border-rose-300/35 bg-rose-950/50 px-3 py-2 text-sm text-rose-100">
              {error}
            </div>
          ) : null}

          <button type="submit" className="btn-primary w-full" disabled={loading}>
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
