import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { MorphLogo } from "@/components/MorphLogo";

// Only ever follow a `next` that is a local, same-origin path — never an
// absolute URL or protocol-relative target — so the OIDC return can't be used
// as an open redirect.
function sanitizeNext(next: string | null): string {
  if (!next) return "/";
  if (!next.startsWith("/") || next.startsWith("//")) return "/";
  return next;
}

// Chrome-less landing for the OIDC return. The backend 302s the browser here
// with the token in the URL FRAGMENT (not the query) so it never hits the
// server logs: `/auth/callback#access_token=...&expires_at=...&tenant_slug=...&next=...`.
export function OidcCallbackPage() {
  const { loginWithToken } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  // The token lives in the fragment, which never changes for this mount — run
  // the exchange exactly once even under React 18 StrictMode double-invoke.
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return;
    handled.current = true;

    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const accessToken = params.get("access_token");
    const expiresAt = params.get("expires_at");
    const tenantSlug = params.get("tenant_slug");
    const next = sanitizeNext(params.get("next"));

    if (!accessToken) {
      setError("Sign-in response was missing a token. Please try signing in again.");
      return;
    }

    try {
      loginWithToken({
        access_token: accessToken,
        token_type: "Bearer",
        expires_at: expiresAt,
        tenant_slug: tenantSlug,
        mfa_required: false,
      });
    } catch {
      setError("Sign-in response was invalid. Please try signing in again.");
      return;
    }

    navigate(next, { replace: true });
  }, [loginWithToken, navigate]);

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

        <div className="card space-y-4 p-6">
          <div>
            <span className="stamp">single sign-on</span>
            <h1 className="headline mt-3 text-xl text-ink-50">
              {error ? "Sign-in failed" : "Signing you in…"}
            </h1>
          </div>

          {error ? (
            <>
              <div className="rounded-control border border-rose-300/35 bg-rose-950/50 px-3 py-2 text-sm text-rose-100">
                {error}
              </div>
              <button
                type="button"
                className="btn-primary w-full"
                onClick={() => navigate("/login", { replace: true })}
              >
                Back to login
              </button>
            </>
          ) : (
            <p className="text-sm text-ink-300">
              Completing your single sign-on. You&apos;ll be redirected in a moment.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
