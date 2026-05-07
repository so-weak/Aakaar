import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Trash2 } from "lucide-react";

import { capabilities as capabilitiesApi, superuser as superuserApi } from "@/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

export function SuperuserTenantDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const queryClient = useQueryClient();

  const tenantsQ = useQuery({
    queryKey: ["superuser", "tenants"],
    queryFn: superuserApi.listTenants,
  });
  const usersQ = useQuery({
    queryKey: ["superuser", "tenants", id, "users"],
    queryFn: () => superuserApi.listTenantUsers(id),
    enabled: !!id,
  });
  const grantsQ = useQuery({
    queryKey: ["superuser", "tenants", id, "grants"],
    queryFn: () => superuserApi.listTenantGrants(id),
    enabled: !!id,
  });
  const capsQ = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: capabilitiesApi.listAll,
  });

  const tenant = useMemo(
    () => tenantsQ.data?.find((t) => t.id === id),
    [tenantsQ.data, id],
  );
  const grantableCaps = useMemo(
    () => (capsQ.data ?? []).filter((c) => c.kind === "capability"),
    [capsQ.data],
  );

  const [capRef, setCapRef] = useState("");
  const [alias, setAlias] = useState("primary");
  const [secrets, setSecrets] = useState<Record<string, string>>({});

  const selectedCap = grantableCaps.find((c) => c.ref === capRef);

  const setCap = (ref: string) => {
    setCapRef(ref);
    const cap = grantableCaps.find((c) => c.ref === ref);
    const next: Record<string, string> = {};
    for (const name of cap?.secret_names ?? []) next[name] = "";
    setSecrets(next);
  };

  const create = useMutation({
    mutationFn: () =>
      superuserApi.createTenantGrant(id, {
        capability_ref: capRef,
        account_alias: alias,
        secrets,
      }),
    onSuccess: () => {
      setCap("");
      setAlias("primary");
      queryClient.invalidateQueries({ queryKey: ["superuser", "tenants", id, "grants"] });
    },
  });

  const remove = useMutation({
    mutationFn: (grantId: string) => superuserApi.deleteTenantGrant(id, grantId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["superuser", "tenants", id, "grants"] }),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={tenant ? `Tenant · ${tenant.name}` : "Tenant"}
        subtitle={
          tenant ? `slug: ${tenant.slug} · status: ${tenant.status}` : "Loading…"
        }
        actions={
          <Link to="/superuser/tenants" className="btn-ghost">
            <ArrowLeft size={14} />
            All tenants
          </Link>
        }
      />

      <div className="relative z-10 grid flex-1 grid-cols-3 gap-6 overflow-hidden p-7">
        <div className="col-span-2 flex flex-col gap-6 overflow-y-auto">
          <section>
            <h2 className="panel-title mb-3">Users</h2>
            {usersQ.error ? <ErrorBanner error={usersQ.error} /> : null}
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Email</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {usersQ.data?.map((u) => (
                  <tr key={u.id}>
                    <td className="px-3 py-2.5 text-ink-100">{u.email}</td>
                    <td className="px-3 py-2.5 text-ink-300">{u.role.replace("_", " ")}</td>
                    <td className="px-3 py-2.5">
                      <span
                        className={
                          u.status === "active"
                            ? "badge ring-emerald-400/40 text-emerald-300"
                            : "badge ring-rose-400/40 text-rose-300"
                        }
                      >
                        {u.status}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-ink-400">
                      {new Date(u.created_at).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {usersQ.data && usersQ.data.length === 0 ? (
              <p className="mt-3 text-sm text-ink-500">No users in this tenant.</p>
            ) : null}
          </section>

          <section>
            <h2 className="panel-title mb-3">Grants</h2>
            {grantsQ.error ? <ErrorBanner error={grantsQ.error} /> : null}
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Capability</th>
                  <th className="px-3 py-2 font-medium">Alias</th>
                  <th className="px-3 py-2 font-medium">Secrets</th>
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
                      {g.secret_names.join(", ") || "—"}
                    </td>
                    <td className="px-3 py-2.5 text-ink-400">
                      {new Date(g.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <button
                        type="button"
                        className="btn-ghost text-rose-300 hover:bg-rose-500/10"
                        onClick={() => remove.mutate(g.id)}
                        disabled={remove.isPending}
                        title="Revoke grant"
                      >
                        <Trash2 size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {grantsQ.data && grantsQ.data.length === 0 ? (
              <p className="mt-3 text-sm text-ink-500">
                No grants yet. Use the form on the right to issue one.
              </p>
            ) : null}
          </section>
        </div>

        <aside className="card h-fit p-5">
          <span className="stamp mb-4">operator grant</span>
          <h2 className="mb-4 text-base font-black uppercase tracking-wide text-ink-50">Issue a grant</h2>
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
            ) : selectedCap ? (
              <p className="text-xs text-ink-500">
                This capability declares no secrets — leave the form blank and submit.
              </p>
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
    </div>
  );
}
