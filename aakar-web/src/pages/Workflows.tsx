import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Plus, Workflow as WorkflowIcon } from "lucide-react";

import { workflows as workflowsApi } from "@/api";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

export function WorkflowsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["workflows"],
    queryFn: workflowsApi.list,
  });

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Workflows"
        subtitle="Saved workflows for your tenant. Anyone can run them; only the creator can edit."
        actions={
          <Link to="/chat" className="btn-primary">
            <Plus size={15} />
            New from chat
          </Link>
        }
      />

      <div className="relative z-10 flex-1 overflow-y-auto p-7">
        {isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : !data || data.length === 0 ? (
          <EmptyState
            title="No workflows yet"
            description="Use the chat to draft your first workflow."
            action={
              <Link to="/chat" className="btn-primary">
                Start chatting
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
                    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-md border border-ink-700 bg-ink-950 text-accent-200 shadow-[4px_4px_0_rgb(22_217_255/0.18)] transition group-hover:border-accent-300">
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
                        <span>{new Date(wf.updated_at).toLocaleString()}</span>
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
