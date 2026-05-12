import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { superuser as superuserApi } from "@/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { useLabels } from "@/i18n/LanguageProvider";
import { formatISTDate } from "@/lib/datetime";

export function SuperuserUsersPage() {
  const labels = useLabels();
  const usersQ = useQuery({
    queryKey: ["superuser", "users"],
    queryFn: superuserApi.listAllUsers,
  });
  const tenantsQ = useQuery({
    queryKey: ["superuser", "tenants"],
    queryFn: superuserApi.listTenants,
  });

  const tenantById = useMemo(() => {
    const m = new Map<string, { slug: string; name: string }>();
    for (const t of tenantsQ.data ?? []) m.set(t.id, { slug: t.slug, name: t.name });
    return m;
  }, [tenantsQ.data]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={`All ${labels.sadhakas.toLowerCase()}`}
        subtitle={`Every ${labels.sadhaka.toLowerCase()} across every ${labels.mandala.toLowerCase()}. Click an ${labels.mandala.toLowerCase()} to drill in.`}
      />
      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        {usersQ.error ? <ErrorBanner error={usersQ.error} /> : null}
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
            <tr>
              <th className="px-3 py-2 font-medium">Email</th>
              <th className="px-3 py-2 font-medium">Role</th>
              <th className="px-3 py-2 font-medium">{labels.mandala}</th>
              <th className="px-3 py-2 font-medium">{labels.status}</th>
              <th className="px-3 py-2 font-medium">Created</th>
            </tr>
          </thead>
          <tbody>
            {usersQ.data?.map((u) => {
              const t = u.tenant_id ? tenantById.get(u.tenant_id) : null;
              return (
                <tr key={u.id}>
                  <td className="px-3 py-2.5 text-ink-100">{u.email}</td>
                  <td className="px-3 py-2.5 text-ink-300">{u.role.replace("_", " ")}</td>
                  <td className="px-3 py-2.5 font-mono text-xs">
                    {u.tenant_id ? (
                      <Link
                        to={`/superuser/tenants/${u.tenant_id}`}
                        className="text-accent-300 hover:text-accent-200"
                      >
                        {t?.slug ?? u.tenant_id}
                      </Link>
                    ) : (
                      <span className="text-ink-500">—</span>
                    )}
                  </td>
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
                    {formatISTDate(u.created_at)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {usersQ.data && usersQ.data.length === 0 ? (
          <p className="mt-6 text-sm text-ink-500">No {labels.sadhakas.toLowerCase()} yet.</p>
        ) : null}
      </div>
    </div>
  );
}
