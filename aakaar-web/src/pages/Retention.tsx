import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Gavel, Lock, LockOpen, Save, Trash2 } from "lucide-react";

import { retention as retentionApi } from "@/api";
import type { RetentionPolicy, RetentionResourceType } from "@/api/types";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDateTime } from "@/lib/datetime";

// The resource types the backend can retain / hold / erase
// (ERASABLE_RESOURCE_TYPES). Labels are presentational only.
const RESOURCE_TYPES: { value: RetentionResourceType; label: string }[] = [
  { value: "run", label: "Runs" },
  { value: "stored_object", label: "Stored objects" },
];

export function RetentionPage() {
  const policiesQ = useQuery({
    queryKey: ["retention", "policies"],
    queryFn: retentionApi.listPolicies,
  });

  // Index existing policies by resource_type so each editor can seed itself.
  const byType = new Map<string, RetentionPolicy>();
  for (const p of policiesQ.data ?? []) byType.set(p.resource_type, p);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Retention &amp; legal hold"
        subtitle="Set how long each kind of data is kept, place legal holds that block deletion, and honour right-to-erasure requests — all scoped to your tenant."
      />
      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        {policiesQ.error ? (
          <div className="mb-4">
            <ErrorBanner error={policiesQ.error} />
          </div>
        ) : null}

        <section className="mb-8">
          <h2 className="panel-title mb-3 flex items-center gap-1.5">
            <CalendarClock size={13} />
            Retention policies
          </h2>
          {policiesQ.isLoading ? (
            <div className="text-sm text-ink-400">Loading…</div>
          ) : (
            <div className="grid gap-4 lg:grid-cols-2">
              {RESOURCE_TYPES.map((rt) => (
                <PolicyEditor
                  key={rt.value}
                  resourceType={rt.value}
                  label={rt.label}
                  policy={byType.get(rt.value) ?? null}
                />
              ))}
            </div>
          )}
        </section>

        <section>
          <h2 className="panel-title mb-3 flex items-center gap-1.5">
            <Gavel size={13} />
            Legal hold &amp; erasure
          </h2>
          <HoldAndErasePanel />
        </section>
      </div>
    </div>
  );
}

// ---------- policy editor -------------------------------------------------

function PolicyEditor({
  resourceType,
  label,
  policy,
}: {
  resourceType: RetentionResourceType;
  label: string;
  policy: RetentionPolicy | null;
}) {
  const queryClient = useQueryClient();
  // "" = retain indefinitely (ttl_days null); otherwise a positive integer.
  const [ttl, setTtl] = useState<string>(
    policy?.ttl_days != null ? String(policy.ttl_days) : "",
  );

  // Re-seed when the server policy loads/changes (e.g. after another tab edit).
  useEffect(() => {
    setTtl(policy?.ttl_days != null ? String(policy.ttl_days) : "");
  }, [policy?.ttl_days]);

  const save = useMutation({
    mutationFn: () => {
      const trimmed = ttl.trim();
      const days = trimmed === "" ? null : Number(trimmed);
      return retentionApi.putPolicy(resourceType, days);
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["retention", "policies"] }),
  });

  const trimmed = ttl.trim();
  const parsed = trimmed === "" ? null : Number(trimmed);
  // Backend requires ttl_days >= 1; mirror that so we don't POST a 400.
  const invalid =
    parsed !== null && (!Number.isInteger(parsed) || parsed < 1);

  return (
    <form
      className="card p-4"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        if (!invalid) save.mutate();
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold text-ink-50">{label}</span>
        <code className="rounded bg-ink-800/80 px-1.5 py-0.5 font-mono text-[11px] text-ink-400">
          {resourceType}
        </code>
      </div>

      <label className="mt-3 block">
        <span className="panel-title">Retain for (days)</span>
        <input
          type="number"
          min={1}
          step={1}
          className="input mt-1 text-sm"
          placeholder="Leave blank to retain indefinitely"
          value={ttl}
          onChange={(e) => setTtl(e.target.value)}
          disabled={save.isPending}
        />
      </label>
      <p className="mt-1 text-[11px] leading-4 text-ink-500">
        {trimmed === ""
          ? "Currently retained indefinitely. Enter a number of days to age this data out."
          : invalid
          ? "Enter a whole number of days (1 or more)."
          : `Older ${label.toLowerCase()} become eligible for cleanup after ${trimmed} day${trimmed === "1" ? "" : "s"}.`}
      </p>

      {policy ? (
        <p className="mt-2 text-[11px] text-ink-600">
          Last updated {formatISTDateTime(policy.updated_at)}
          {policy.updated_by ? ` by ${policy.updated_by.slice(0, 8)}…` : ""}
        </p>
      ) : (
        <p className="mt-2 text-[11px] text-ink-600">No policy set yet.</p>
      )}

      {save.error ? (
        <div className="mt-2">
          <ErrorBanner error={save.error} />
        </div>
      ) : null}

      <button
        type="submit"
        className="btn-primary mt-3 w-full"
        disabled={save.isPending || invalid}
      >
        <Save size={14} />
        {save.isPending ? "Saving…" : "Save policy"}
      </button>
    </form>
  );
}

// ---------- legal hold + erase --------------------------------------------

function HoldAndErasePanel() {
  const [resourceType, setResourceType] = useState<RetentionResourceType>("run");
  const [resourceId, setResourceId] = useState("");
  const [reason, setReason] = useState("");
  const [confirmErase, setConfirmErase] = useState(false);

  const reset = () => {
    hold.reset();
    release.reset();
    erase.reset();
  };

  const hold = useMutation({
    mutationFn: () =>
      retentionApi.setLegalHold({
        resource_type: resourceType,
        resource_id: resourceId.trim(),
        hold: true,
      }),
  });
  const release = useMutation({
    mutationFn: () =>
      retentionApi.setLegalHold({
        resource_type: resourceType,
        resource_id: resourceId.trim(),
        hold: false,
      }),
  });
  const erase = useMutation({
    mutationFn: () =>
      retentionApi.erase({
        resource_type: resourceType,
        resource_id: resourceId.trim(),
        reason: reason.trim() || undefined,
      }),
    onSuccess: () => setConfirmErase(false),
  });

  const busy = hold.isPending || release.isPending || erase.isPending;
  const idValid = isUuid(resourceId.trim());
  const actionError = hold.error ?? release.error ?? erase.error ?? null;

  return (
    <div className="card max-w-2xl p-4">
      <div className="grid gap-3 sm:grid-cols-[160px_1fr]">
        <label className="block">
          <span className="panel-title">Resource type</span>
          <select
            className="input mt-1 text-sm"
            value={resourceType}
            onChange={(e) => {
              setResourceType(e.target.value as RetentionResourceType);
              reset();
            }}
            disabled={busy}
          >
            {RESOURCE_TYPES.map((rt) => (
              <option key={rt.value} value={rt.value}>
                {rt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="panel-title">Resource ID</span>
          <input
            type="text"
            className="input mt-1 font-mono text-xs"
            placeholder="UUID of the run / object"
            value={resourceId}
            onChange={(e) => {
              setResourceId(e.target.value);
              setConfirmErase(false);
              reset();
            }}
            disabled={busy}
            spellCheck={false}
          />
        </label>
      </div>

      {resourceId.trim() !== "" && !idValid ? (
        <p className="mt-1.5 text-[11px] text-amber-300">
          That doesn't look like a UUID — copy the id from the run or object.
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-ghost text-amber-300 hover:bg-amber-500/10"
          onClick={() => {
            reset();
            hold.mutate();
          }}
          disabled={busy || !idValid}
          title="Block deletion of this resource until the hold is cleared"
        >
          <Lock size={15} />
          {hold.isPending ? "Setting…" : "Set legal hold"}
        </button>
        <button
          type="button"
          className="btn-ghost"
          onClick={() => {
            reset();
            release.mutate();
          }}
          disabled={busy || !idValid}
          title="Clear an existing legal hold"
        >
          <LockOpen size={15} />
          {release.isPending ? "Clearing…" : "Clear hold"}
        </button>
        <div className="mx-1 h-5 w-px bg-ink-700" />
        <button
          type="button"
          className="btn-ghost text-rose-300 hover:bg-rose-500/10"
          onClick={() => {
            if (!confirmErase) {
              setConfirmErase(true);
              return;
            }
            reset();
            erase.mutate();
          }}
          disabled={busy || !idValid}
          title="Permanently erase this resource (blocked if under legal hold)"
        >
          <Trash2 size={15} />
          {erase.isPending
            ? "Erasing…"
            : confirmErase
            ? "Click again to erase"
            : "Erase"}
        </button>
      </div>

      {confirmErase ? (
        <label className="mt-3 block">
          <span className="panel-title">Erasure reason (optional)</span>
          <input
            type="text"
            className="input mt-1 text-xs"
            placeholder="e.g. data subject request #1234"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            maxLength={512}
          />
        </label>
      ) : null}

      <p className="mt-3 text-[11px] leading-4 text-ink-500">
        Erasure is permanent and tenant-scoped. A resource under legal hold
        can't be erased until the hold is cleared. Holds and erasures are
        recorded in the audit log.
      </p>

      {hold.isSuccess ? (
        <ResultLine ok text="Legal hold set." />
      ) : null}
      {release.isSuccess ? (
        <ResultLine ok text="Legal hold cleared." />
      ) : null}
      {erase.data ? (
        <ResultLine
          ok
          text={
            erase.data.already_erased
              ? "Already erased — nothing more to do."
              : `Erased at ${formatISTDateTime(erase.data.erased_at)}.`
          }
        />
      ) : null}
      {actionError ? (
        <div className="mt-2">
          <ErrorBanner error={actionError} />
        </div>
      ) : null}
    </div>
  );
}

function ResultLine({ ok, text }: { ok: boolean; text: string }) {
  return (
    <div
      className={[
        "mt-2 rounded border px-3 py-2 text-xs",
        ok
          ? "border-emerald-400/30 bg-emerald-950/30 text-emerald-200"
          : "border-rose-400/30 bg-rose-950/30 text-rose-100",
      ].join(" ")}
    >
      {text}
    </div>
  );
}

// Loose RFC-4122 UUID check — enough to stop obvious typos before the POST.
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value,
  );
}
