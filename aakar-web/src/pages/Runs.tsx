import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { runs as runsApi } from "@/api";
import type { RunStatus } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

const STATUS_STYLES: Record<RunStatus, { ring: string; text: string; dot: string }> = {
  queued: { ring: "ring-ink-700", text: "text-ink-300", dot: "bg-ink-500" },
  running: { ring: "ring-accent-500/40", text: "text-accent-300", dot: "bg-accent-400 animate-pulse" },
  paused: { ring: "ring-amber-400/40", text: "text-amber-300", dot: "bg-amber-400" },
  succeeded: { ring: "ring-emerald-400/40", text: "text-emerald-300", dot: "bg-emerald-400" },
  failed: { ring: "ring-rose-400/40", text: "text-rose-300", dot: "bg-rose-400" },
  cancelled: { ring: "ring-ink-700", text: "text-ink-400", dot: "bg-ink-500" },
};

export function RunsPage() {
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["runs"],
    queryFn: runsApi.list,
    refetchInterval: 4_000,
  });

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Runs"
        subtitle="Live and historical workflow runs."
        actions={
          <button
            type="button"
            className="btn-ghost"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            Refresh
          </button>
        }
      />
      <div className="relative z-10 flex-1 overflow-y-auto p-7">
        {isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : !data || data.length === 0 ? (
          <EmptyState
            title="No runs yet"
            description="Open a workflow and hit Run to start one."
          />
        ) : (
          <table className="w-full table-fixed text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
              <tr>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Run</th>
                <th className="px-3 py-2 font-medium">Workflow</th>
                <th className="px-3 py-2 font-medium">Started</th>
                <th className="px-3 py-2 font-medium">Ended</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => {
                const styles = STATUS_STYLES[r.status];
                return (
                  <tr key={r.id}>
                    <td className="rounded-l-md px-3 py-2.5">
                      <span className={["badge", styles.ring, styles.text].join(" ")}>
                        <span className={["h-1.5 w-1.5 rounded-full", styles.dot].join(" ")} />
                        {r.status}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <Link
                        to={`/runs/${r.id}`}
                        className="font-mono text-xs text-ink-200 hover:text-ink-50"
                      >
                        {r.id.slice(0, 8)}…
                      </Link>
                    </td>
                    <td className="px-3 py-2.5">
                      <Link
                        to={`/workflows/${r.workflow_id}`}
                        className="text-ink-200 hover:text-ink-50"
                      >
                        {r.workflow_id.slice(0, 8)}…
                      </Link>{" "}
                      <span className="text-ink-500">v{r.workflow_version}</span>
                    </td>
                    <td className="px-3 py-2.5 text-ink-400">
                      {new Date(r.started_at).toLocaleString()}
                    </td>
                    <td className="rounded-r-md px-3 py-2.5 text-ink-400">
                      {r.ended_at ? new Date(r.ended_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
