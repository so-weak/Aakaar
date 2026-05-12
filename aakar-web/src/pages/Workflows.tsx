import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Plus, Workflow as WorkflowIcon } from "lucide-react";

import { workflows as workflowsApi } from "@/api";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { useLabels } from "@/i18n/LanguageProvider";
import { formatISTDateTime } from "@/lib/datetime";

export function WorkflowsPage() {
  const labels = useLabels();
  const { data, isLoading, error } = useQuery({
    queryKey: ["workflows"],
    queryFn: workflowsApi.list,
  });

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={labels.sutras}
        subtitle={`Saved ${labels.sutras.toLowerCase()} for your ${labels.mandala.toLowerCase()}. Any ${labels.sadhaka.toLowerCase()} may run one; only the author may edit.`}
        actions={
          <Link to="/chat" className="btn-primary">
            <Plus size={15} />
            New from {labels.samvada.toLowerCase()}
          </Link>
        }
      />

      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        {isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : !data || data.length === 0 ? (
          <EmptyState
            title={`No ${labels.sutras.toLowerCase()} yet`}
            description={`Open the ${labels.samvada.toLowerCase()} to compose your first ${labels.sutra.toLowerCase()}.`}
            action={
              <Link to="/chat" className="btn-primary">
                Open {labels.samvada.toLowerCase()}
              </Link>
            }
          />
        ) : (
          <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
            {data.map((wf) => (
              <li key={wf.id}>
                <Link
                  to={`/workflows/${wf.id}`}
                  className="card group block p-4 transition hover:-translate-y-0.5 hover:border-accent-300/45 hover:bg-ink-800/65"
                >
                  <div className="flex items-start gap-3">
                    <span className="brand-shadow-cyan-md grid h-10 w-10 shrink-0 place-items-center rounded-control border border-ink-700 bg-ink-950 text-accent-200 transition group-hover:border-accent-300">
                      <WorkflowIcon size={16} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-ink-50">
                        {wf.name}
                      </div>
                      <div className="mt-0.5 truncate text-xs text-ink-400">
                        {wf.description || "No description"}
                      </div>
                      <div className="mt-3 flex items-center gap-3 font-mono text-[11px] uppercase tracking-wide text-ink-500">
                        <span>v{wf.latest_version}</span>
                        <span className="text-signal-pink">/</span>
                        <span>{formatISTDateTime(wf.updated_at)}</span>
                      </div>
                    </div>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
