import { admin as adminApi, capabilities as capabilitiesApi } from "@/api";
import { PageHeader } from "@/components/PageHeader";
import { VaultSections, type VaultApi } from "@/components/Vault";

const tenantAdminApi: VaultApi = {
  queryKeyBase: ["admin", "grants"],
  listGrants: adminApi.listGrants,
  createGrant: adminApi.createGrant,
  updateGrant: adminApi.updateGrant,
  deleteGrant: adminApi.deleteGrant,
  listCapabilities: capabilitiesApi.list,
  capabilitiesQueryKey: ["capabilities"],
};

/**
 * Tenant-admin's vault page. The page itself is just the header + a
 * scoping wrapper; the real UI lives in `VaultSections` so the
 * super-admin tenant-detail page can reuse the exact same flow.
 */
export function AdminGrantsPage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Vault"
        subtitle="Sites your workflows can log into, and the capabilities they're allowed to use."
      />
      <div className="relative z-10 flex-1 overflow-y-auto p-7">
        <VaultSections api={tenantAdminApi} />
      </div>
    </div>
  );
}
