import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";

import { capabilities as capabilitiesApi, superuser as superuserApi } from "@/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { VaultSections, type VaultApi } from "@/components/Vault";
import { formatISTDate } from "@/lib/datetime";

export function SuperuserTenantDetailPage() {
  const { id = "" } = useParams<{ id: string }>();

  const tenantsQ = useQuery({
    queryKey: ["superuser", "tenants"],
    queryFn: superuserApi.listTenants,
  });
  const usersQ = useQuery({
    queryKey: ["superuser", "tenants", id, "users"],
    queryFn: () => superuserApi.listTenantUsers(id),
    enabled: !!id,
  });

  const tenant = useMemo(
    () => tenantsQ.data?.find((t) => t.id === id),
    [tenantsQ.data, id],
  );

  // Adapter that wraps the superuser endpoints into the same VaultApi
  // shape the tenant-admin page uses. Same UI, same form, just scoped
  // to a specific tenant id.
  const vaultApi: VaultApi = useMemo(
    () => ({
      queryKeyBase: ["superuser", "tenants", id, "grants"],
      listGrants: () => superuserApi.listTenantGrants(id),
      createGrant: (input) => superuserApi.createTenantGrant(id, input),
      updateGrant: (grantId, patch) =>
        superuserApi.updateTenantGrant(id, grantId, patch),
      deleteGrant: (grantId) => superuserApi.deleteTenantGrant(id, grantId),
      listCapabilities: capabilitiesApi.listAll,
      capabilitiesQueryKey: ["capabilities", "all"],
    }),
    [id],
  );

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

      <div className="relative z-10 flex-1 space-y-8 overflow-y-auto p-7">
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
                  <td className="px-3 py-2.5 text-ink-300">
                    {u.role.replace("_", " ")}
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
              ))}
            </tbody>
          </table>
          {usersQ.data && usersQ.data.length === 0 ? (
            <p className="mt-3 text-sm text-ink-500">No users in this tenant.</p>
          ) : null}
        </section>

        <VaultSections api={vaultApi} />
      </div>
    </div>
  );
}
