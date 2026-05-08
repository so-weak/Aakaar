import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";

import {
  runs as runsApi,
  superuser as superuserApi,
  workflows as workflowsApi,
} from "@/api";
import type { Run, RunDetail, RunStatus, Tenant, WorkflowVersion } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBanner } from "@/components/ErrorBanner";
import { LiveDagViewer } from "@/components/LiveDagViewer";
import { PageHeader } from "@/components/PageHeader";
import { formatISTTime } from "@/lib/datetime";

/**
 * Operator console: a tile-grid view of all active runs.
 *  - tenant_admin: all active runs in their own tenant.
 *  - superuser:    all active runs across every tenant (tenant slug shown).
 *
 * Each tile is a compact LiveDagViewer keyed by the run id. The list
 * polls every 2s; per-run RunDetail polls every 2s; workflow versions
 * (DAG bodies) are cached forever — they're immutable per (id, version).
 */
export function LiveProcessesPage() {
  const { claims } = useAuth();
  const isSuper = claims?.role === "superuser";

  const runsQ = useQuery({
    queryKey: isSuper ? ["live-runs", "all"] : ["live-runs", "tenant"],
    queryFn: () =>
      isSuper
        ? superuserApi.listAllRuns({ active: true })
        : runsApi.list({ active: true }),
    refetchInterval: 2000,
  });

  const tenantsQ = useQuery({
    queryKey: ["superuser", "tenants"],
    queryFn: superuserApi.listTenants,
    enabled: isSuper,
    staleTime: 60_000,
  });

  const tenantsById = useMemo(() => {
    const m = new Map<string, Tenant>();
    for (const t of tenantsQ.data ?? []) m.set(t.id, t);
    return m;
  }, [tenantsQ.data]);

  const runs = runsQ.data ?? [];

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Live processes"
        subtitle={
          isSuper
            ? `Cross-tenant view · ${runs.length} active`
            : `${runs.length} active in your tenant`
        }
        actions={
          <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-signal-cyan">
            <Activity size={10} className="mr-1 inline animate-pulse" />
            polling 2s
          </span>
        }
      />

      <div className="relative z-10 flex-1 overflow-y-auto p-7">
        {runsQ.error ? <ErrorBanner error={runsQ.error} /> : null}

        {runs.length === 0 && !runsQ.isLoading ? (
          <div className="card mx-auto max-w-md p-8 text-center">
            <h3 className="mb-1 text-base font-semibold text-ink-100">
              No active runs
            </h3>
            <p className="text-sm text-ink-400">
              {isSuper
                ? "Nothing is running across any tenant right now."
                : "Nothing is running. Start a workflow from Workflows or Chat."}
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {runs.map((r) => (
              <RunTile
                key={r.id}
                run={r}
                tenantSlug={
                  isSuper ? tenantsById.get(r.tenant_id)?.slug ?? null : null
                }
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- tile --------------------------------------------------------

function RunTile({
  run,
  tenantSlug,
}: {
  run: Run;
  tenantSlug: string | null;
}) {
  // Per-tile: poll events (so the live indicator advances) + load DAG once.
  const detailQ = useQuery<RunDetail>({
    queryKey: ["run", run.id],
    queryFn: () => runsApi.get(run.id),
    refetchInterval: (q) => {
      const r = q.state.data?.run;
      if (!r) return 2_000;
      const done =
        r.status === "succeeded" ||
        r.status === "failed" ||
        r.status === "cancelled";
      return done ? false : 2_000;
    },
  });

  const versionQ = useQuery<WorkflowVersion>({
    queryKey: ["workflow-version", run.workflow_id, run.workflow_version],
    queryFn: () => workflowsApi.getVersion(run.workflow_id, run.workflow_version),
    staleTime: Infinity,
  });

  const workflowQ = useQuery({
    queryKey: ["workflow", run.workflow_id],
    queryFn: () => workflowsApi.get(run.workflow_id),
    staleTime: 60_000,
    // Only the run's own tenant can resolve the workflow name through
    // /workflows/{id}; superuser doesn't have a cross-tenant workflow
    // endpoint, so we just show "wf <id>" in that case.
    enabled: tenantSlug === null,
  });

  const status = (detailQ.data?.run.status ?? run.status) as RunStatus;
  const events = detailQ.data?.events ?? [];

  return (
    <Link
      to={`/runs/${run.id}`}
      className="card group block overflow-hidden p-0 transition hover:ring-1 hover:ring-accent-300/40"
    >
      <div className="flex items-center justify-between gap-2 border-b border-ink-700/70 px-4 py-2.5">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-ink-100">
            {workflowQ.data?.name ?? `wf ${run.workflow_id.slice(0, 8)}`}
            <span className="ml-1.5 font-mono text-[10px] text-ink-500">
              v{run.workflow_version}
            </span>
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[11px] text-ink-500">
            {tenantSlug ? (
              <span className="badge ring-signal-pink/30 text-signal-pink">
                {tenantSlug}
              </span>
            ) : null}
            <span className="font-mono">{run.id.slice(0, 8)}</span>
            <span>·</span>
            <span>{formatISTTime(run.started_at)} IST</span>
          </div>
        </div>
        <RunStatusBadge status={status} />
      </div>

      <div className="h-[220px] bg-ink-950/45">
        {versionQ.data ? (
          <LiveDagViewer
            dag={versionQ.data.dag}
            events={events}
            runStatus={status}
            compact
          />
        ) : versionQ.error ? (
          <div className="grid h-full place-items-center px-4 text-center text-xs text-rose-300">
            DAG unavailable
          </div>
        ) : (
          <div className="grid h-full place-items-center text-xs text-ink-500">
            Loading DAG…
          </div>
        )}
      </div>
    </Link>
  );
}

function RunStatusBadge({ status }: { status: RunStatus }) {
  const map: Record<RunStatus, string> = {
    queued: "ring-ink-700 text-ink-300",
    running: "ring-signal-cyan/40 text-signal-cyan",
    paused: "ring-amber-400/40 text-amber-300",
    succeeded: "ring-emerald-400/40 text-emerald-300",
    failed: "ring-rose-400/40 text-rose-300",
    cancelled: "ring-ink-700 text-ink-400",
  };
  return <span className={`badge ${map[status]}`}>{status}</span>;
}
