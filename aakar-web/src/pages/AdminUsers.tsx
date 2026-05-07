import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { admin as adminApi } from "@/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const usersQ = useQuery({ queryKey: ["admin", "users"], queryFn: adminApi.listUsers });

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"tenant_admin" | "tenant_user">("tenant_user");

  const create = useMutation({
    mutationFn: () => adminApi.createUser({ email, password, role }),
    onSuccess: () => {
      setEmail("");
      setPassword("");
      queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Users" subtitle="Tenant members. Admins can invite more users." />
      <div className="relative z-10 grid flex-1 grid-cols-3 gap-6 overflow-hidden p-7">
        <section className="col-span-2 overflow-y-auto">
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
                  <td className="px-3 py-2.5 text-ink-300">{u.status}</td>
                  <td className="px-3 py-2.5 text-ink-400">
                    {new Date(u.created_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <aside className="card h-fit p-5">
          <span className="stamp mb-4">member press</span>
          <h2 className="mb-4 text-base font-black uppercase tracking-wide text-ink-50">Invite a user</h2>
          <form onSubmit={onSubmit} className="space-y-3">
            <label className="block">
              <span className="panel-title">Email</span>
              <input
                className="input mt-1"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </label>
            <label className="block">
              <span className="panel-title">Password</span>
              <input
                className="input mt-1"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={8}
                required
              />
            </label>
            <label className="block">
              <span className="panel-title">Role</span>
              <select
                className="input mt-1"
                value={role}
                onChange={(e) => setRole(e.target.value as typeof role)}
              >
                <option value="tenant_user">Tenant user</option>
                <option value="tenant_admin">Tenant admin</option>
              </select>
            </label>
            {create.error ? <ErrorBanner error={create.error} /> : null}
            <button type="submit" className="btn-primary w-full" disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create user"}
            </button>
          </form>
        </aside>
      </div>
    </div>
  );
}
