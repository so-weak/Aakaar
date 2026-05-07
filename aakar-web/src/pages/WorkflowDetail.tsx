import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { Play, Trash2 } from "lucide-react";

import { runs as runsApi, workflows as workflowsApi } from "@/api";
import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { DagViewer } from "@/components/DagViewer";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

export function WorkflowDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { claims } = useAuth();

  const workflowQ = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => workflowsApi.get(id),
    enabled: !!id,
  });
  const versionQ = useQuery({
    queryKey: ["workflow", id, "latest"],
    queryFn: () => workflowsApi.getLatestVersion(id),
    enabled: !!id,
  });

  const start = useMutation({
    mutationFn: () => runsApi.start(id, {}),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/runs/${run.id}`);
    },
  });

  const remove = useMutation({
    mutationFn: () => workflowsApi.remove(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate("/workflows");
    },
  });

  const [confirmDelete, setConfirmDelete] = useState(false);

  if (workflowQ.isLoading || versionQ.isLoading) {
    return <div className="p-7 text-sm text-ink-400">Loading…</div>;
  }
  if (workflowQ.error || versionQ.error) {
    return (
      <div className="p-7">
        <ErrorBanner error={workflowQ.error ?? versionQ.error} />
      </div>
    );
  }
  const workflow = workflowQ.data!;
  const version = versionQ.data!;
  const isOwner = claims?.user_id === workflow.created_by;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={workflow.name}
        subtitle={`v${version.version} · ${workflow.description || "No description"}`}
        actions={
          <>
            {isOwner ? (
              <button
                type="button"
                className="btn-ghost text-rose-300 hover:bg-rose-500/10"
                onClick={() => {
                  if (!confirmDelete) {
                    setConfirmDelete(true);
                    return;
                  }
                  remove.mutate();
                }}
                disabled={remove.isPending}
              >
                <Trash2 size={15} />
                {confirmDelete ? "Click again to confirm" : "Delete"}
              </button>
            ) : null}
            <button
              type="button"
              className="btn-primary"
              onClick={() => start.mutate()}
              disabled={start.isPending}
            >
              <Play size={15} /> Run
            </button>
          </>
        }
      />

      {start.error ? (
        <div className="border-b border-ink-800 p-3">
          <ErrorBanner error={describeStartError(start.error)} />
        </div>
      ) : null}

      <div className="relative z-10 flex-1">
        <DagViewer dag={version.dag} />
      </div>
    </div>
  );
}

function describeStartError(err: unknown): string {
  if (err instanceof ApiError) return err.detail || err.message;
  return err instanceof Error ? err.message : String(err);
}
