import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { runs as runsApi } from "@/api";
import type { RunStatus } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { useLabels, useRunStatusLabel } from "@/i18n/LanguageProvider";
import { formatISTDateTime } from "@/lib/datetime";

const STATUS_STYLES: Record<RunStatus, { ring: string; text: string; dot: string }> = {
  queued: { ring: "ring-ink-700", text: "text-ink-300", dot: "bg-ink-500" },
  running: { ring: "ring-accent-500/40", text: "text-accent-300", dot: "bg-accent-400 animate-pulse" },
  paused: { ring: "ring-amber-400/40", text: "text-amber-300", dot: "bg-amber-400" },
  succeeded: { ring: "ring-emerald-400/40", text: "text-emerald-300", dot: "bg-emerald-400" },
  failed: { ring: "ring-rose-400/40", text: "text-rose-300", dot: "bg-rose-400" },
  cancelled: { ring: "ring-ink-700", text: "text-ink-400", dot: "bg-ink-500" },
};

export function RunsPage() {
  const labels = useLabels();
  const runStatusLabel = useRunStatusLabel();
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["runs"],
    queryFn: runsApi.list,
    refetchInterval: 4_000,
  });

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={labels.yajnas}
        subtitle={`Live and historical ${labels.sutra.toLowerCase()} runs.`}
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
      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        {isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : !data || data.length === 0 ? (
          <EmptyState
            title={`No ${labels.yajnas.toLowerCase()} yet`}
            description={`Open a ${labels.sutra.toLowerCase()} and run one to begin.`}
          />
        ) : (
          <table className="w-full table-fixed text-sm">
            <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
              <tr>
                <th className="px-3 py-2 font-medium">{labels.status}</th>
                <th className="px-3 py-2 font-medium">{labels.yajna}</th>
                <th className="px-3 py-2 font-medium">{labels.sutra}</th>
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
                        {runStatusLabel(r.status)}
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
                      {formatISTDateTime(r.started_at)}
                    </td>
                    <td className="rounded-r-md px-3 py-2.5 text-ink-400">
                      {r.ended_at ? formatISTDateTime(r.ended_at) : "—"}
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
