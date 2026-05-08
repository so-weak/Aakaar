import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";

import type { Grant } from "@/api/types";
import { ErrorBanner } from "@/components/ErrorBanner";

type UpdatePayload = {
  account_alias?: string;
  secrets?: Record<string, string>;
  input_defaults?: Record<string, unknown>;
  enabled?: boolean;
};

interface GrantEditModalProps {
  grant: Grant;
  /** Display-only label; used to show "Editing X" so the user knows what they're rotating. */
  capabilityLabel?: string;
  /** Mutation function that takes the patch body and returns the updated grant. */
  onSave: (patch: UpdatePayload) => Promise<Grant>;
  onClose: () => void;
  /** Refresh queries on success — caller decides what to invalidate. */
  onSuccess: () => void;
}

/**
 * Edit modal for a vault credential grant.
 *
 * Secrets are blank by default — submitting without filling them in
 * leaves the existing vault entry untouched. To rotate, the user must
 * tick "Rotate secrets" and fill ALL declared fields (the backend
 * rejects partial rotation).
 */
export function GrantEditModal({
  grant,
  capabilityLabel,
  onSave,
  onClose,
  onSuccess,
}: GrantEditModalProps) {
  const initialDefaultsJson = useMemo(
    () => JSON.stringify(grant.input_defaults ?? {}, null, 2),
    [grant.input_defaults],
  );

  const [alias, setAlias] = useState(grant.account_alias);
  const [enabled, setEnabled] = useState(grant.enabled);
  const [rotating, setRotating] = useState(false);
  const [secrets, setSecrets] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const name of grant.secret_names) init[name] = "";
    return init;
  });
  const [defaultsJson, setDefaultsJson] = useState(initialDefaultsJson);
  const [defaultsError, setDefaultsError] = useState<string | null>(null);

  // If the upstream grant changes (rare — the modal usually mounts fresh),
  // reset to the new defaults so we don't show stale state.
  useEffect(() => {
    setAlias(grant.account_alias);
    setEnabled(grant.enabled);
    setRotating(false);
    const init: Record<string, string> = {};
    for (const name of grant.secret_names) init[name] = "";
    setSecrets(init);
    setDefaultsJson(initialDefaultsJson);
    setDefaultsError(null);
  }, [
    grant.id,
    grant.account_alias,
    grant.enabled,
    grant.secret_names,
    initialDefaultsJson,
  ]);

  const update = useMutation({
    mutationFn: (patch: UpdatePayload) => onSave(patch),
    onSuccess: () => {
      onSuccess();
      onClose();
    },
  });

  const dirty = useMemo(() => {
    if (alias !== grant.account_alias) return true;
    if (enabled !== grant.enabled) return true;
    if (rotating && Object.values(secrets).every((v) => v.length > 0)) return true;
    if (defaultsJson.trim() !== initialDefaultsJson.trim()) return true;
    return false;
  }, [alias, enabled, rotating, secrets, defaultsJson, initialDefaultsJson, grant]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();

    let parsedDefaults: Record<string, unknown> | undefined;
    if (defaultsJson.trim() !== initialDefaultsJson.trim()) {
      try {
        const parsed = JSON.parse(defaultsJson || "{}");
        if (
          parsed === null ||
          typeof parsed !== "object" ||
          Array.isArray(parsed)
        ) {
          setDefaultsError("Default inputs must be a JSON object.");
          return;
        }
        parsedDefaults = parsed as Record<string, unknown>;
        setDefaultsError(null);
      } catch (err) {
        setDefaultsError(
          err instanceof Error ? `Invalid JSON: ${err.message}` : "Invalid JSON.",
        );
        return;
      }
    }

    const patch: UpdatePayload = {};
    if (alias !== grant.account_alias) patch.account_alias = alias;
    if (enabled !== grant.enabled) patch.enabled = enabled;
    if (rotating) patch.secrets = secrets;
    if (parsedDefaults !== undefined) patch.input_defaults = parsedDefaults;
    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }
    update.mutate(patch);
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 backdrop-blur">
      <div className="card w-full max-w-md p-5">
        <h3 className="mb-1 text-base font-semibold text-ink-50">Edit credential</h3>
        <p className="mb-4 text-xs text-ink-400">
          {capabilityLabel ? (
            <>
              Editing{" "}
              <span className="font-mono text-ink-200">
                {capabilityLabel}:{grant.account_alias}
              </span>
              .
            </>
          ) : (
            <>
              Editing{" "}
              <span className="font-mono text-ink-200">
                {grant.capability_ref}:{grant.account_alias}
              </span>
              .
            </>
          )}{" "}
          Secret values are never returned by the API; rotate them by ticking the box below.
        </p>

        <form onSubmit={onSubmit} className="space-y-3">
          <label className="block">
            <span className="panel-title">Account alias</span>
            <input
              className="input mt-1"
              value={alias}
              onChange={(e) => setAlias(e.target.value)}
              pattern="^[a-z][a-z0-9_-]*$"
              required
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-ink-200">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            Enabled — uncheck to hide this credential from the planner without revoking.
          </label>

          {grant.secret_names.length > 0 ? (
            <fieldset className="space-y-2 rounded-md border border-ink-700 p-3">
              <legend className="panel-title px-1">Secrets</legend>
              <label className="flex items-center gap-2 text-xs text-ink-300">
                <input
                  type="checkbox"
                  checked={rotating}
                  onChange={(e) => setRotating(e.target.checked)}
                />
                Rotate secrets (must fill every field below)
              </label>
              {grant.secret_names.map((name) => (
                <label key={name} className="block">
                  <span className="text-xs text-ink-400">{name}</span>
                  <input
                    type="password"
                    className="input mt-0.5"
                    value={secrets[name] ?? ""}
                    onChange={(e) =>
                      setSecrets((prev) => ({ ...prev, [name]: e.target.value }))
                    }
                    placeholder={rotating ? "New value" : "•••••••• (unchanged)"}
                    disabled={!rotating}
                    required={rotating}
                  />
                </label>
              ))}
            </fieldset>
          ) : null}

          <label className="block">
            <span className="panel-title">Default inputs (JSON)</span>
            <textarea
              className="input mt-1 font-mono text-xs"
              rows={4}
              value={defaultsJson}
              onChange={(e) => {
                setDefaultsJson(e.target.value);
                if (defaultsError) setDefaultsError(null);
              }}
              spellCheck={false}
              placeholder={'{\n  "login_url": "https://..."\n}'}
            />
            <span className="mt-1 block text-[11px] text-ink-500">
              Site-specific defaults the planner pre-fills (e.g.{" "}
              <span className="font-mono text-ink-400">login_url</span>). Must be a JSON object.
            </span>
            {defaultsError ? (
              <span className="mt-1 block text-xs text-rose-300">{defaultsError}</span>
            ) : null}
          </label>

          {update.error ? <ErrorBanner error={update.error} /> : null}

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              className="btn-ghost"
              onClick={onClose}
              disabled={update.isPending}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={update.isPending || !dirty}
            >
              {update.isPending ? "Saving…" : "Save changes"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
