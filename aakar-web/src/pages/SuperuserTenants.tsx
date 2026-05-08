import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { superuser as superuserApi } from "@/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDate } from "@/lib/datetime";

export function SuperuserTenantsPage() {
  const queryClient = useQueryClient();
  const tenantsQ = useQuery({
    queryKey: ["superuser", "tenants"],
    queryFn: superuserApi.listTenants,
  });

  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [adminPassword, setAdminPassword] = useState("");

  const create = useMutation({
    mutationFn: () =>
      superuserApi.createTenant({
        slug,
        name,
        admin_email: adminEmail,
        admin_password: adminPassword,
      }),
    onSuccess: () => {
      setSlug("");
      setName("");
      setAdminEmail("");
      setAdminPassword("");
      queryClient.invalidateQueries({ queryKey: ["superuser", "tenants"] });
    },
  });

  const suspend = useMutation({
    mutationFn: (id: string) => superuserApi.suspendTenant(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["superuser", "tenants"] }),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Tenants"
        subtitle="Register a new tenant and seed its first admin user."
      />
      <div className="relative z-10 grid flex-1 grid-cols-3 gap-6 overflow-hidden p-7">
        <section className="col-span-2 overflow-y-auto">
          {tenantsQ.error ? <ErrorBanner error={tenantsQ.error} /> : null}
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
              <tr>
                <th className="px-3 py-2 font-medium">Slug</th>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {tenantsQ.data?.map((t) => (
                <tr key={t.id}>
                  <td className="px-3 py-2.5 font-mono text-xs">
                    <Link
                      to={`/superuser/tenants/${t.id}`}
                      className="text-accent-300 hover:text-accent-200"
                    >
                      {t.slug}
                    </Link>
                  </td>
                  <td className="px-3 py-2.5 text-ink-100">{t.name}</td>
                  <td className="px-3 py-2.5">
                    <span
                      className={
                        t.status === "active"
                          ? "badge ring-emerald-400/40 text-emerald-300"
                          : "badge ring-rose-400/40 text-rose-300"
                      }
                    >
                      {t.status}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-ink-400">
                    {formatISTDate(t.created_at)}
                  </td>
                  <td className="px-3 py-2.5 text-right">
                    {t.status === "active" ? (
                      <button
                        type="button"
                        className="btn-ghost text-rose-300 hover:bg-rose-500/10"
                        onClick={() => suspend.mutate(t.id)}
                        disabled={suspend.isPending}
                      >
                        Suspend
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <aside className="card h-fit p-5">
          <span className="stamp mb-4">new scene</span>
          <h2 className="mb-4 text-base font-black uppercase tracking-wide text-ink-50">Register a tenant</h2>
          <form onSubmit={onSubmit} className="space-y-3">
            <label className="block">
              <span className="panel-title">Slug</span>
              <input
                className="input mt-1"
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                pattern="^[a-z][a-z0-9-]*$"
                minLength={2}
                required
              />
            </label>
            <label className="block">
              <span className="panel-title">Name</span>
              <input
                className="input mt-1"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </label>
            <label className="block">
              <span className="panel-title">
                Admin email
              </span>
              <input
                className="input mt-1"
                type="email"
                value={adminEmail}
                onChange={(e) => setAdminEmail(e.target.value)}
                required
              />
            </label>
            <label className="block">
              <span className="panel-title">
                Admin password
              </span>
              <input
                className="input mt-1"
                type="password"
                value={adminPassword}
                onChange={(e) => setAdminPassword(e.target.value)}
                minLength={8}
                required
              />
            </label>
            {create.error ? <ErrorBanner error={create.error} /> : null}
            <button type="submit" className="btn-primary w-full" disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create tenant"}
            </button>
          </form>
        </aside>
      </div>
    </div>
  );
}
