import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";

import { admin as adminApi, capabilities as capabilitiesApi } from "@/api";
import type { Grant } from "@/api/types";
import { ErrorBanner } from "@/components/ErrorBanner";
import { GrantEditModal } from "@/components/GrantEditModal";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDate } from "@/lib/datetime";

export function AdminGrantsPage() {
  const queryClient = useQueryClient();

  const grantsQ = useQuery({ queryKey: ["admin", "grants"], queryFn: adminApi.listGrants });
  const capsQ = useQuery({ queryKey: ["capabilities"], queryFn: capabilitiesApi.list });

  const grantableCaps = useMemo(
    () => (capsQ.data ?? []).filter((c) => c.kind === "capability"),
    [capsQ.data],
  );

  const [capRef, setCapRef] = useState("");
  const [alias, setAlias] = useState("primary");
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<Grant | null>(null);

  const selectedCap = grantableCaps.find((c) => c.ref === capRef);

  const setCap = (ref: string) => {
    setCapRef(ref);
    const cap = grantableCaps.find((c) => c.ref === ref);
    if (cap) {
      const next: Record<string, string> = {};
      for (const name of cap.secret_names) next[name] = "";
      setSecrets(next);
    } else {
      setSecrets({});
    }
  };

  const create = useMutation({
    mutationFn: () =>
      adminApi.createGrant({
        capability_ref: capRef,
        account_alias: alias,
        secrets,
      }),
    onSuccess: () => {
      setCap("");
      setAlias("primary");
      queryClient.invalidateQueries({ queryKey: ["admin", "grants"] });
    },
  });

  const del = useMutation({
    mutationFn: (id: string) => adminApi.deleteGrant(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "grants"] }),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Capability grants"
        subtitle="Bind staff-defined capabilities to credential aliases for your tenant. Secrets are vault-stored and never returned by the API."
      />

      <div className="relative z-10 grid flex-1 grid-cols-3 gap-6 overflow-hidden p-7">
        <section className="col-span-2 overflow-y-auto">
          {grantsQ.error ? <ErrorBanner error={grantsQ.error} /> : null}
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
              <tr>
                <th className="px-3 py-2 font-medium">Capability</th>
                <th className="px-3 py-2 font-medium">Alias</th>
                <th className="px-3 py-2 font-medium">Secrets (masked)</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {grantsQ.data?.map((g) => (
                <tr key={g.id}>
                  <td className="px-3 py-2.5 font-mono text-xs text-ink-100">
                    {g.capability_ref}
                  </td>
                  <td className="px-3 py-2.5 text-ink-300">{g.account_alias}</td>
                  <td className="px-3 py-2.5 text-ink-400">
                    {g.secret_names.length
                      ? g.secret_names.map((n) => `${n}: ••••••••`).join("  ·  ")
                      : "—"}
                  </td>
                  <td className="px-3 py-2.5">
                    <span
                      className={
                        g.enabled
                          ? "badge ring-emerald-400/40 text-emerald-300"
                          : "badge ring-amber-400/40 text-amber-300"
                      }
                    >
                      {g.enabled ? "enabled" : "paused"}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-ink-400">
                    {formatISTDate(g.created_at)}
                  </td>
                  <td className="px-3 py-2.5 text-right">
                    <div className="flex justify-end gap-1">
                      <button
                        type="button"
                        className="btn-ghost"
                        onClick={() => setEditing(g)}
                        title="Edit credential"
                      >
                        <Pencil size={14} />
                      </button>
                      <button
                        type="button"
                        className="btn-ghost text-rose-300 hover:bg-rose-500/10"
                        onClick={() => del.mutate(g.id)}
                        disabled={del.isPending}
                        title="Revoke grant"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {grantsQ.data && grantsQ.data.length === 0 ? (
            <p className="mt-6 text-sm text-ink-500">
              No grants yet. Pick a capability on the right to create one.
            </p>
          ) : null}
        </section>

        <aside className="card h-fit p-5">
          <span className="stamp mb-4">vault tape</span>
          <h2 className="mb-4 text-base font-black uppercase tracking-wide text-ink-50">Grant a capability</h2>
          <form onSubmit={onSubmit} className="space-y-3">
            <label className="block">
              <span className="panel-title">
                Capability
              </span>
              <select
                className="input mt-1"
                value={capRef}
                onChange={(e) => setCap(e.target.value)}
                required
              >
                <option value="">Select a capability…</option>
                {grantableCaps.map((c) => (
                  <option key={c.ref} value={c.ref}>
                    {c.ref}
                  </option>
                ))}
              </select>
              {selectedCap ? (
                <p className="mt-1.5 text-xs text-ink-500">{selectedCap.description}</p>
              ) : null}
            </label>

            <label className="block">
              <span className="panel-title">
                Account alias
              </span>
              <input
                className="input mt-1"
                value={alias}
                onChange={(e) => setAlias(e.target.value)}
                pattern="^[a-z][a-z0-9_-]*$"
                required
              />
            </label>

            {selectedCap?.secret_names.length ? (
              <fieldset className="space-y-2 rounded-md border border-ink-700 p-3">
                <legend className="panel-title px-1">
                  Secrets
                </legend>
                {selectedCap.secret_names.map((name) => (
                  <label key={name} className="block">
                    <span className="text-xs text-ink-400">{name}</span>
                    <input
                      type="password"
                      className="input mt-0.5"
                      value={secrets[name] ?? ""}
                      onChange={(e) =>
                        setSecrets((prev) => ({ ...prev, [name]: e.target.value }))
                      }
                      required
                    />
                  </label>
                ))}
              </fieldset>
            ) : null}

            {create.error ? <ErrorBanner error={create.error} /> : null}

            <button
              type="submit"
              className="btn-primary w-full"
              disabled={!capRef || create.isPending}
            >
              {create.isPending ? "Granting…" : "Grant"}
            </button>
          </form>
        </aside>
      </div>

      {editing ? (
        <GrantEditModal
          grant={editing}
          capabilityLabel={editing.capability_ref}
          onSave={(patch) => adminApi.updateGrant(editing.id, patch)}
          onClose={() => setEditing(null)}
          onSuccess={() =>
            queryClient.invalidateQueries({ queryKey: ["admin", "grants"] })
          }
        />
      ) : null}
    </div>
  );
}
