import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { capabilities as capabilitiesApi } from "@/api";
import type { CapabilityDefinitionResponse, NodeKind } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

const KIND_ORDER: NodeKind[] = ["capability", "action", "control"];

const KIND_BADGES: Record<NodeKind, string> = {
  capability: "ring-emerald-400/40 text-emerald-300",
  action: "ring-accent-400/40 text-accent-300",
  control: "ring-amber-400/40 text-amber-300",
};

export function CapabilitiesPage() {
  const { claims } = useAuth();
  const isSuperuser = claims?.role === "superuser";

  const { data, isLoading, error } = useQuery({
    queryKey: ["capabilities", isSuperuser ? "all" : "granted"],
    queryFn: isSuperuser ? capabilitiesApi.listAll : capabilitiesApi.list,
  });

  const headings: Record<NodeKind, string> = {
    capability: isSuperuser ? "All capabilities" : "Granted capabilities",
    action: "Action primitives",
    control: "Control nodes",
  };

  const grouped = useMemo(() => {
    const acc: Record<NodeKind, CapabilityDefinitionResponse[]> = {
      capability: [],
      action: [],
      control: [],
    };
    for (const item of data ?? []) acc[item.kind].push(item);
    return acc;
  }, [data]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Capabilities"
        subtitle={
          isSuperuser
            ? "Every capability registered in the codebase, plus action and control primitives. Capabilities still need a per-tenant grant before a tenant can plan with them."
            : "What the planner can reach for. Capabilities are tenant-granted; primitives are always available."
        }
      />
      <div className="relative z-10 flex-1 overflow-y-auto p-7">
        {isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : (
          <div className="space-y-8">
            {KIND_ORDER.map((kind) =>
              grouped[kind].length === 0 ? null : (
                <section key={kind}>
                  <h2 className="panel-title mb-3">
                    {headings[kind]}{" "}
                    <span className="ml-1 text-ink-600">({grouped[kind].length})</span>
                  </h2>
                  <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                    {grouped[kind].map((c) => (
                      <li key={c.ref} className="card p-4 transition hover:border-accent-300/35">
                        <div className="flex items-start justify-between gap-3">
                          <code className="font-mono text-sm text-ink-100">{c.ref}</code>
                          <span className={`badge ${KIND_BADGES[kind]}`}>{kind}</span>
                        </div>
                        <p className="mt-3 text-sm leading-6 text-ink-300">{c.description}</p>
                        {c.secret_names.length > 0 ? (
                          <p className="mt-2 text-xs text-ink-500">
                            Secrets:{" "}
                            <span className="text-ink-300">
                              {c.secret_names.join(", ")}
                            </span>
                          </p>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </section>
              ),
            )}
          </div>
        )}
      </div>
    </div>
  );
}
