import { useState } from "react";
import type { FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Globe, ShieldCheck } from "lucide-react";

import { auth as authApi } from "@/api";
import { ApiError } from "@/api/client";
import type { LoginResponse } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { MorphLogo } from "@/components/MorphLogo";
import { useLabels } from "@/i18n/LanguageProvider";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

export function LoginPage() {
  const { login, loginWithToken, claims, loading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const labels = useLabels();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Set to the short-lived ticket once the password check passes but a second
  // factor is still required — flips the form to the code-entry step.
  const [mfaToken, setMfaToken] = useState<string | null>(null);

  if (claims) return <Navigate to={from} replace />;

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const resp = await login(email, password);
      if (resp.mfa_required && resp.mfa_token) {
        setMfaToken(resp.mfa_token);
        return;
      }
      navigate(from, { replace: true });
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : (err as Error).message;
      setError(detail || "Login failed");
    }
  };

  // Full-page redirect to the IdP — this is a 302 to the provider, not a
  // fetch, so we navigate the browser rather than calling the API client.
  const onSso = () => {
    window.location.href = `${API_BASE}/auth/oidc/login`;
  };

  if (mfaToken) {
    return (
      <MfaChallenge
        mfaToken={mfaToken}
        email={email}
        onVerified={(resp) => {
          loginWithToken(resp, email);
          navigate(from, { replace: true });
        }}
        onCancel={() => {
          setMfaToken(null);
          setError(null);
        }}
      />
    );
  }

  return (
    <div className="noise-shell app-shell grid h-full place-items-center overflow-hidden px-4">
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
              className="logo-tile brand-shadow-pink-lg grid h-16 w-16 place-items-center rounded-control"
              aria-hidden="true"
            >
              <MorphLogo />
            </span>
          </div>
          <div className="text-center">
            <div className="headline text-4xl text-ink-50">AAKAAR</div>
            <div className="mt-2 font-mono text-[11px] uppercase tracking-[0.28em] text-accent-200">
              the workshop of forms
            </div>
          </div>
        </div>

        <form onSubmit={onSubmit} className="card space-y-4 p-6">
          <div>
            <span className="stamp">secure desk</span>
            <h1 className="headline mt-3 text-xl text-ink-50">{labels.pravesha}</h1>
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
            <div className="rounded-control border border-rose-300/35 bg-rose-950/50 px-3 py-2 text-sm text-rose-100">
              {error}
            </div>
          ) : null}

          <button type="submit" className="btn-primary w-full" disabled={loading}>
            {loading ? labels.praveshing : labels.pravesha}
          </button>

          {/* SSO entry point — full-page redirect to the configured IdP. The
              backend 302s to the provider and, on return, to /auth/callback. */}
          <div className="flex items-center gap-3 pt-1">
            <span className="h-px flex-1 bg-ink-700/60" />
            <span className="panel-title text-ink-500">or</span>
            <span className="h-px flex-1 bg-ink-700/60" />
          </div>
          <button
            type="button"
            className="btn-ghost w-full justify-center"
            onClick={onSso}
          >
            <Globe size={15} />
            Sign in with SSO
          </button>
        </form>
      </div>
    </div>
  );
}

// Second step of password login: the password check passed but MFA is on, so
// the server handed us a short-lived `mfa_token` ticket. The user enters a
// TOTP code (or a one-time recovery code) which we exchange for a real session.
function MfaChallenge({
  mfaToken,
  email,
  onVerified,
  onCancel,
}: {
  mfaToken: string;
  email: string;
  onVerified: (resp: LoginResponse) => void;
  onCancel: () => void;
}) {
  const [code, setCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setVerifying(true);
    try {
      const resp = await authApi.mfaVerify({
        mfa_token: mfaToken,
        ...(useRecovery ? { recovery_code: code.trim() } : { code: code.trim() }),
      });
      onVerified(resp);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : (err as Error).message;
      setError(detail || "Verification failed");
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div className="noise-shell app-shell grid h-full place-items-center overflow-hidden px-4">
      <div className="w-full max-w-md">
        <div className="mb-8">
          <div className="mb-5 flex items-center justify-center">
            <span
              className="logo-tile brand-shadow-pink-lg grid h-16 w-16 place-items-center rounded-control"
              aria-hidden="true"
            >
              <MorphLogo />
            </span>
          </div>
          <div className="text-center">
            <div className="headline text-4xl text-ink-50">AAKAAR</div>
            <div className="mt-2 font-mono text-[11px] uppercase tracking-[0.28em] text-accent-200">
              the workshop of forms
            </div>
          </div>
        </div>

        <form onSubmit={onSubmit} className="card space-y-4 p-6">
          <div>
            <span className="stamp">second factor</span>
            <h1 className="headline mt-3 flex items-center gap-2 text-xl text-ink-50">
              <ShieldCheck size={18} className="text-accent-300" />
              Verify it&apos;s you
            </h1>
            <p className="mt-1 text-sm text-ink-300">
              Enter the{" "}
              {useRecovery ? "recovery" : "6-digit"} code for{" "}
              <span className="font-mono text-ink-100">{email}</span>.
            </p>
          </div>

          <label className="block">
            <span className="panel-title">
              {useRecovery ? "Recovery code" : "Authenticator code"}
            </span>
            <input
              type="text"
              inputMode={useRecovery ? "text" : "numeric"}
              autoComplete="one-time-code"
              autoFocus
              required
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="input mt-1 font-mono tracking-[0.3em]"
              placeholder={useRecovery ? "xxxx-xxxx-xxxx" : "123456"}
              spellCheck={false}
            />
          </label>

          {error ? (
            <div className="rounded-control border border-rose-300/35 bg-rose-950/50 px-3 py-2 text-sm text-rose-100">
              {error}
            </div>
          ) : null}

          <button
            type="submit"
            className="btn-primary w-full"
            disabled={verifying || !code.trim()}
          >
            {verifying ? "Verifying…" : "Verify"}
          </button>

          <div className="flex items-center justify-between pt-1 text-[11px]">
            <button
              type="button"
              className="text-ink-400 hover:text-ink-200"
              onClick={() => {
                setUseRecovery((v) => !v);
                setCode("");
                setError(null);
              }}
            >
              {useRecovery
                ? "Use an authenticator code instead"
                : "Use a recovery code instead"}
            </button>
            <button
              type="button"
              className="text-ink-400 hover:text-ink-200"
              onClick={onCancel}
            >
              Back to login
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
