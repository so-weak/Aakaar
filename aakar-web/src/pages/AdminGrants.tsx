import { admin as adminApi, capabilities as capabilitiesApi } from "@/api";
import { PageHeader } from "@/components/PageHeader";
import { VaultSections, type VaultApi } from "@/components/Vault";
import { useLabels } from "@/i18n/LanguageProvider";

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
  const labels = useLabels();
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={labels.kosha}
        subtitle={`Sites your ${labels.mandala.toLowerCase()} may enter, and the ${labels.vidyas.toLowerCase()} it is granted to wield.`}
      />
      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        <VaultSections api={tenantAdminApi} />
      </div>
    </div>
  );
}
