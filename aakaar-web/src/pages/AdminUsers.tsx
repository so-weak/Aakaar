import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, KeyRound, Pause, PencilLine, PlayCircle } from "lucide-react";

import { admin as adminApi } from "@/api";
import type { User } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { useLabels } from "@/i18n/LanguageProvider";
import { formatISTDate } from "@/lib/datetime";

type Role = "tenant_admin" | "tenant_user";

export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const { claims } = useAuth();
  const labels = useLabels();
  const usersQ = useQuery({ queryKey: ["admin", "users"], queryFn: adminApi.listUsers });

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("tenant_user");
  const [editing, setEditing] = useState<User | null>(null);

  const create = useMutation({
    mutationFn: () => adminApi.createUser({ email, password, role }),
    onSuccess: () => {
      setEmail("");
      setPassword("");
      queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
    },
  });

  const suspend = useMutation({
    mutationFn: (id: string) => adminApi.suspendUser(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "users"] }),
  });

  const reactivate = useMutation({
    mutationFn: (id: string) => adminApi.reactivateUser(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "users"] }),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader title={labels.sadhakas} subtitle={`Members of your ${labels.mandala.toLowerCase()}. The ${labels.acharya.toLowerCase()} may invite, edit, and suspend.`} />
      <div className="relative z-10 grid min-h-0 flex-1 grid-cols-3 gap-6 overflow-hidden p-7">
        <section className="col-span-2 min-h-0 overflow-y-auto">
          {usersQ.error ? <ErrorBanner error={usersQ.error} /> : null}
          {suspend.error ? <ErrorBanner error={suspend.error} /> : null}
          {reactivate.error ? <ErrorBanner error={reactivate.error} /> : null}
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
              <tr>
                <th className="px-3 py-2 font-medium">Email</th>
                <th className="px-3 py-2 font-medium">Role</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {usersQ.data?.map((u) => {
                const isSelf = claims?.user_id === u.id;
                const isDisabled = u.status === "disabled";
                return (
                  <tr key={u.id}>
                    <td className="px-3 py-2.5 text-ink-100">
                      {u.email}
                      {isSelf ? (
                        <span className="ml-2 text-[10px] uppercase tracking-wider text-ink-500">
                          you
                        </span>
                      ) : null}
                    </td>
                    <td className="px-3 py-2.5 text-ink-300">{u.role.replace("_", " ")}</td>
                    <td className="px-3 py-2.5">
                      <span
                        className={
                          isDisabled
                            ? "badge ring-rose-400/40 text-rose-300"
                            : "badge ring-emerald-400/40 text-emerald-300"
                        }
                      >
                        {u.status}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-ink-400">
                      {formatISTDate(u.created_at)}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      {isSelf ? (
                        <span className="text-[11px] text-ink-500">—</span>
                      ) : (
                        <span className="flex justify-end gap-1">
                          <button
                            type="button"
                            className="btn-ghost"
                            onClick={() => setEditing(u)}
                            title="Edit role / reset password"
                          >
                            <PencilLine size={14} />
                          </button>
                          {isDisabled ? (
                            <button
                              type="button"
                              className="btn-ghost text-emerald-300 hover:bg-emerald-500/10"
                              onClick={() => reactivate.mutate(u.id)}
                              disabled={reactivate.isPending}
                              title="Reactivate"
                            >
                              <PlayCircle size={14} />
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="btn-ghost text-amber-300 hover:bg-amber-500/10"
                              onClick={() => {
                                if (
                                  window.confirm(
                                    `Suspend ${u.email}? They will be logged out on their next request and cannot log in until reactivated.`,
                                  )
                                ) {
                                  suspend.mutate(u.id);
                                }
                              }}
                              disabled={suspend.isPending}
                              title="Suspend"
                            >
                              <Pause size={14} />
                            </button>
                          )}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>

        <aside className="card h-fit p-5">
          <span className="stamp mb-4">member press</span>
          <h2 className="mb-4 text-base font-black uppercase tracking-wide text-ink-50">
            Initiate a {labels.sadhaka.toLowerCase()}
          </h2>
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
                onChange={(e) => setRole(e.target.value as Role)}
              >
                <option value="tenant_user">{labels.sadhaka}</option>
                <option value="tenant_admin">{labels.acharya}</option>
              </select>
            </label>
            {create.error ? <ErrorBanner error={create.error} /> : null}
            <button type="submit" className="btn-primary w-full" disabled={create.isPending}>
              {create.isPending ? "Initiating…" : `Initiate ${labels.sadhaka.toLowerCase()}`}
            </button>
          </form>
        </aside>
      </div>

      {editing ? (
        <EditUserModal
          user={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
          }}
        />
      ) : null}
    </div>
  );
}

function EditUserModal({
  user,
  onClose,
  onSaved,
}: {
  user: User;
  onClose: () => void;
  onSaved: () => void;
}) {
  const labels = useLabels();
  const [role, setRole] = useState<Role>(
    user.role === "tenant_admin" ? "tenant_admin" : "tenant_user",
  );
  const [password, setPassword] = useState("");

  const update = useMutation({
    mutationFn: () => {
      const body: { role?: Role; password?: string } = {};
      if (role !== user.role) body.role = role;
      if (password) body.password = password;
      return adminApi.updateUser(user.id, body);
    },
    onSuccess: onSaved,
  });

  const noOp = role === user.role && !password;
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!noOp) update.mutate();
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 backdrop-blur">
      <div className="card w-full max-w-md p-5">
        <h3 className="mb-1 text-base font-semibold text-ink-50">Edit {labels.sadhaka.toLowerCase()}</h3>
        <p className="mb-4 text-xs text-ink-400">
          Editing <span className="font-mono text-ink-200">{user.email}</span>. Email cannot be
          changed; initiate a new {labels.sadhaka.toLowerCase()} instead.
        </p>
        <form onSubmit={onSubmit} className="space-y-3">
          <label className="block">
            <span className="panel-title">Role</span>
            <select
              className="input mt-1"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
            >
              <option value="tenant_user">{labels.sadhaka}</option>
              <option value="tenant_admin">{labels.acharya}</option>
            </select>
          </label>
          <label className="block">
            <span className="panel-title">
              <KeyRound size={11} className="mr-1 inline" />
              New password
            </span>
            <input
              className="input mt-1"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Leave blank to keep existing"
              minLength={password ? 8 : undefined}
            />
            <span className="mt-1 block text-[11px] text-ink-500">
              Min 8 characters. Issuing a new password does NOT invalidate existing JWT tokens
              automatically — they expire on their own schedule.
            </span>
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
              disabled={update.isPending || noOp}
            >
              <CheckCircle2 size={14} />
              {update.isPending ? "Saving…" : "Save changes"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
