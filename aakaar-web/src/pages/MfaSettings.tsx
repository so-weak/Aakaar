import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  CheckCircle2,
  Copy,
  KeyRound,
  LogOut,
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
} from "lucide-react";

import { auth as authApi } from "@/api";
import type { MfaConfirmResponse, MfaEnrollResponse } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

const MFA_STATUS_KEY = ["auth", "mfa", "status"];

export function MfaSettingsPage() {
  const statusQ = useQuery({ queryKey: MFA_STATUS_KEY, queryFn: authApi.mfaStatus });

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Two-factor authentication"
        subtitle="Add a time-based one-time passcode (TOTP) as a second factor. Once enabled, a code from your authenticator app is required at every sign-in."
      />
      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        <div className="mx-auto max-w-xl space-y-5">
          {statusQ.isLoading ? (
            <div className="text-sm text-ink-400">Loading…</div>
          ) : statusQ.error ? (
            <ErrorBanner error={statusQ.error} />
          ) : statusQ.data?.enabled ? (
            <DisablePanel />
          ) : (
            <EnrollFlow pending={statusQ.data?.pending ?? false} />
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- enable flow ----------------------------------------------------

type EnrollStep =
  | { phase: "idle" }
  | { phase: "enrolled"; data: MfaEnrollResponse }
  | { phase: "confirmed"; codes: string[] };

function EnrollFlow({ pending }: { pending: boolean }) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<EnrollStep>({ phase: "idle" });
  const [code, setCode] = useState("");

  const enroll = useMutation({
    mutationFn: () => authApi.mfaEnroll(),
    onSuccess: (data) => setStep({ phase: "enrolled", data }),
  });

  const confirm = useMutation({
    mutationFn: (input: string) => authApi.mfaConfirm(input),
    onSuccess: (res: MfaConfirmResponse) => {
      setStep({ phase: "confirmed", codes: res.recovery_codes });
      // Enabling MFA invalidates the current pwd-only token on the next
      // request — refresh status so a remount reflects the new state.
      queryClient.invalidateQueries({ queryKey: MFA_STATUS_KEY });
    },
  });

  const onConfirm = (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim() || confirm.isPending) return;
    confirm.mutate(code.trim());
  };

  if (step.phase === "confirmed") {
    return <RecoveryCodes codes={step.codes} />;
  }

  return (
    <>
      <div className="card p-5">
        <span className="stamp mb-3">disabled</span>
        <h2 className="headline flex items-center gap-2 text-lg text-ink-50">
          <ShieldOff size={18} className="text-ink-400" />
          Two-factor is off
        </h2>
        <p className="mt-2 text-sm leading-6 text-ink-300">
          {pending
            ? "You have a pending enrollment. Scan the secret below with your authenticator app and confirm a code to finish turning it on."
            : "Protect your account with an authenticator app (Google Authenticator, 1Password, Authy, …). You'll scan a secret once, then enter a 6-digit code at every sign-in."}
        </p>
        {step.phase === "idle" ? (
          <div className="mt-4">
            {enroll.error ? (
              <div className="mb-3">
                <ErrorBanner error={enroll.error} />
              </div>
            ) : null}
            <button
              type="button"
              className="btn-primary"
              onClick={() => enroll.mutate()}
              disabled={enroll.isPending}
            >
              <ShieldCheck size={15} />
              {enroll.isPending ? "Starting…" : "Set up two-factor"}
            </button>
          </div>
        ) : null}
      </div>

      {step.phase === "enrolled" ? (
        <div className="card space-y-4 p-5">
          <div>
            <span className="panel-title">Step 1 — add the secret</span>
            <p className="mt-1 text-sm leading-6 text-ink-300">
              Open your authenticator app and add a new account using the secret
              below, or open the provisioning link on the device that has the app.
            </p>
          </div>

          <SecretField label="Secret key" value={step.data.secret} mono />
          <SecretField
            label="Provisioning link (otpauth)"
            value={step.data.otpauth_url}
          />

          <p className="text-[11px] leading-5 text-ink-500">
            Tip: most apps let you paste the secret manually. The provisioning
            link is the same data an app would read from a QR code.
          </p>

          <form onSubmit={onConfirm} className="space-y-3 border-t border-ink-700/60 pt-4">
            <label className="block">
              <span className="panel-title">Step 2 — confirm a code</span>
              <input
                className="input mt-1 font-mono tracking-[0.3em]"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="123456"
                spellCheck={false}
                required
              />
              <span className="mt-1 block text-[11px] text-ink-500">
                Enter the current 6-digit code from your authenticator to finish.
              </span>
            </label>
            {confirm.error ? <ErrorBanner error={confirm.error} /> : null}
            <button
              type="submit"
              className="btn-primary w-full"
              disabled={confirm.isPending || !code.trim()}
            >
              <CheckCircle2 size={15} />
              {confirm.isPending ? "Confirming…" : "Confirm and enable"}
            </button>
          </form>
        </div>
      ) : null}
    </>
  );
}

// Shown ONCE, right after MFA is confirmed. Enabling MFA invalidates the
// current pwd-only token, so we surface the recovery codes and then push the
// user back to a fresh login.
function RecoveryCodes({ codes }: { codes: string[] }) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [copied, setCopied] = useState(false);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(codes.join("\n"));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard may be unavailable (insecure context); the codes are still
      // selectable below.
      setCopied(false);
    }
  };

  const onReLogin = () => {
    logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="card space-y-4 p-5">
      <div>
        <span className="stamp mb-3">enabled</span>
        <h2 className="headline flex items-center gap-2 text-lg text-ink-50">
          <ShieldCheck size={18} className="text-emerald-300" />
          Two-factor is on
        </h2>
      </div>

      <div className="brand-shadow-pink-sm flex items-start gap-2 rounded-control border border-amber-300/35 bg-amber-950/40 px-3 py-2 text-sm text-amber-100">
        <ShieldAlert size={16} className="mt-0.5 shrink-0" />
        <span>
          Save these recovery codes now — they are shown only once. Each code
          works a single time if you lose access to your authenticator.
        </span>
      </div>

      <ul className="grid grid-cols-2 gap-2 rounded-control border border-ink-700/60 bg-ink-900/40 p-3">
        {codes.map((c) => (
          <li key={c} className="font-mono text-sm tracking-wide text-ink-100">
            {c}
          </li>
        ))}
      </ul>

      <div className="flex items-center justify-between gap-2">
        <button type="button" className="btn-ghost" onClick={onCopy}>
          {copied ? (
            <CheckCircle2 size={14} className="text-emerald-300" />
          ) : (
            <Copy size={14} />
          )}
          {copied ? "Copied" : "Copy codes"}
        </button>
        <button type="button" className="btn-primary" onClick={onReLogin}>
          <LogOut size={14} />
          Re-login with two-factor
        </button>
      </div>

      <p className="text-[11px] leading-5 text-ink-500">
        Enabling two-factor signed out your current session. Log back in to
        verify your authenticator works.
      </p>
    </div>
  );
}

// ---------- disable panel --------------------------------------------------

function DisablePanel() {
  const queryClient = useQueryClient();
  const [code, setCode] = useState("");

  const disable = useMutation({
    mutationFn: (input: string) => authApi.mfaDisable(input),
    onSuccess: () => {
      setCode("");
      queryClient.invalidateQueries({ queryKey: MFA_STATUS_KEY });
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim() || disable.isPending) return;
    disable.mutate(code.trim());
  };

  return (
    <div className="card space-y-4 p-5">
      <div>
        <span className="stamp mb-3">enabled</span>
        <h2 className="headline flex items-center gap-2 text-lg text-ink-50">
          <ShieldCheck size={18} className="text-emerald-300" />
          Two-factor is on
        </h2>
        <p className="mt-2 text-sm leading-6 text-ink-300">
          A code from your authenticator is required at every sign-in. To turn
          two-factor off, confirm a current code below.
        </p>
      </div>

      <form onSubmit={onSubmit} className="space-y-3 border-t border-ink-700/60 pt-4">
        <label className="block">
          <span className="panel-title">
            <KeyRound size={11} className="mr-1 inline" />
            Authenticator code
          </span>
          <input
            className="input mt-1 font-mono tracking-[0.3em]"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="123456"
            spellCheck={false}
            required
          />
        </label>
        {disable.error ? <ErrorBanner error={disable.error} /> : null}
        <button
          type="submit"
          className="btn-ghost text-rose-300 hover:bg-rose-500/10"
          disabled={disable.isPending || !code.trim()}
        >
          <ShieldOff size={15} />
          {disable.isPending ? "Disabling…" : "Disable two-factor"}
        </button>
      </form>
    </div>
  );
}

// ---------- helpers --------------------------------------------------------

function SecretField({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div>
      <span className="panel-title">{label}</span>
      <div className="mt-1 flex items-stretch gap-2">
        <input
          readOnly
          value={value}
          onFocus={(e) => e.currentTarget.select()}
          className={mono ? "input flex-1 font-mono text-xs" : "input flex-1 text-xs"}
          aria-label={label}
        />
        <button
          type="button"
          className="btn-ghost shrink-0"
          onClick={onCopy}
          title={`Copy ${label.toLowerCase()}`}
        >
          {copied ? (
            <CheckCircle2 size={14} className="text-emerald-300" />
          ) : (
            <Copy size={14} />
          )}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}
