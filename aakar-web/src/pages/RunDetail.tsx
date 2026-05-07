import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { CheckCircle2, Circle, Pause, PlayCircle, Send, XCircle } from "lucide-react";

import { runs as runsApi } from "@/api";
import type { PendingPrompt, RunDetail, RunEvent } from "@/api/types";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

export function RunDetailPage() {
  const { id = "" } = useParams<{ id: string }>();

  const { data, isLoading, error } = useQuery<RunDetail>({
    queryKey: ["run", id],
    queryFn: () => runsApi.get(id),
    refetchInterval: (q) => {
      const r = q.state.data?.run;
      if (!r) return 1_500;
      if (r.status === "succeeded" || r.status === "failed" || r.status === "cancelled")
        return false;
      return 1_500;
    },
    enabled: !!id,
  });

  if (isLoading) return <div className="p-7 text-sm text-ink-400">Loading…</div>;
  if (error)
    return (
      <div className="p-7">
        <ErrorBanner error={error} />
      </div>
    );
  if (!data) return null;

  const { run, events, pending_prompts } = data;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={`Run ${run.id.slice(0, 8)}`}
        subtitle={
          <>
            Workflow{" "}
            <span className="font-mono text-ink-300">
              {run.workflow_id.slice(0, 8)}
            </span>{" "}
            · v{run.workflow_version} · started {new Date(run.started_at).toLocaleString()}
          </>
        }
        actions={<StatusPill status={run.status} />}
      />

      <div className="relative z-10 grid flex-1 grid-cols-3 gap-0 overflow-hidden">
        <section className="col-span-2 flex flex-col overflow-hidden border-r border-ink-700/80">
          <div className="border-b border-ink-700/80 bg-ink-950/45 px-6 py-3 panel-title">
            Timeline
          </div>
          <div className="flex-1 overflow-y-auto px-6 py-4">
            {events.length === 0 ? (
              <div className="text-sm text-ink-400">No events yet.</div>
            ) : (
              <ol className="space-y-2">
                {events.map((e) => (
                  <EventRow key={e.sequence} event={e} />
                ))}
              </ol>
            )}
          </div>
        </section>

        <aside className="flex flex-col overflow-y-auto bg-ink-950/45 px-5 py-5 backdrop-blur">
          {pending_prompts.length > 0 ? (
            <section className="mb-5">
              <h3 className="panel-title mb-2 text-amber-300">
                Pending prompts
              </h3>
              <div className="space-y-3">
                {pending_prompts.map((p) => (
                  <PromptCard key={p.node_id} runId={run.id} prompt={p} />
                ))}
              </div>
            </section>
          ) : null}

          {run.error ? (
            <section className="mb-5">
              <h3 className="panel-title mb-2 text-rose-300">
                Error
              </h3>
              <div className="card p-3 text-sm">
                <div className="mb-1 text-xs text-ink-500">{run.error.type}</div>
                <div className="text-ink-100">{run.error.message}</div>
              </div>
            </section>
          ) : null}

          <section>
            <h3 className="panel-title mb-2">
              Outputs
            </h3>
            {Object.keys(run.outputs).length === 0 ? (
              <div className="text-xs text-ink-500">None yet.</div>
            ) : (
              <div className="card overflow-x-auto p-3">
                <pre className="font-mono text-xs leading-5 text-ink-200">
                  {JSON.stringify(run.outputs, null, 2)}
                </pre>
              </div>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}

// ---------- bits ---------------------------------------------------------

function StatusPill({ status }: { status: RunDetail["run"]["status"] }) {
  const map = {
    queued: "ring-ink-700 text-ink-300",
    running: "ring-accent-500/40 text-accent-300",
    paused: "ring-amber-400/40 text-amber-300",
    succeeded: "ring-emerald-400/40 text-emerald-300",
    failed: "ring-rose-400/40 text-rose-300",
    cancelled: "ring-ink-700 text-ink-400",
  } as const;
  return <span className={`badge ${map[status]}`}>{status}</span>;
}

function EventRow({ event }: { event: RunEvent }) {
  const Icon =
    event.kind === "node_completed"
      ? CheckCircle2
      : event.kind === "node_failed"
      ? XCircle
      : event.kind === "run_paused"
      ? Pause
      : event.kind === "run_resumed"
      ? PlayCircle
      : Circle;

  const color =
    event.kind === "node_completed"
      ? "text-emerald-400"
      : event.kind === "node_failed"
      ? "text-rose-400"
      : event.kind === "run_paused"
      ? "text-amber-400"
      : event.kind === "run_resumed"
      ? "text-accent-400"
      : "text-ink-500";

  return (
    <li className="flex gap-3">
      <div className="flex flex-col items-center pt-0.5">
        <Icon size={14} className={color} />
        <div className="mt-1 h-full w-px bg-ink-700/70" />
      </div>
      <div className="flex-1 pb-3">
        <div className="flex items-baseline justify-between gap-2 text-xs text-ink-400">
          <span>
            <span className="text-ink-200">{event.kind}</span>
            {event.node_id ? (
              <>
                {" "}
                · <span className="font-mono">{event.node_id}</span>
              </>
            ) : null}
          </span>
          <time>{new Date(event.at).toLocaleTimeString()}</time>
        </div>
        {Object.keys(event.payload).length > 0 ? (
          <pre className="mt-2 overflow-x-auto rounded border border-ink-700 bg-ink-950/70 px-2 py-1.5 font-mono text-[11px] leading-5 text-ink-300">
            {JSON.stringify(event.payload, null, 2)}
          </pre>
        ) : null}
      </div>
    </li>
  );
}

function PromptCard({ runId, prompt }: { runId: string; prompt: PendingPrompt }) {
  const [value, setValue] = useState("");
  const queryClient = useQueryClient();

  const respond = useMutation({
    mutationFn: () =>
      runsApi.respond(runId, { node_id: prompt.node_id, response: value }),
    onSuccess: () => {
      setValue("");
      queryClient.invalidateQueries({ queryKey: ["run", runId] });
    },
  });

  return (
    <div className="card p-3">
      <div className="mb-2 flex items-center justify-between text-xs">
        <span className="font-mono text-ink-400">{prompt.node_id}</span>
        <span className="badge ring-amber-400/40 text-amber-300">{prompt.expects}</span>
      </div>
      <p className="mb-2 text-sm text-ink-100">{prompt.message}</p>
      <div className="flex gap-2">
        <input
          type={prompt.expects === "otp" ? "text" : "text"}
          inputMode={prompt.expects === "otp" ? "numeric" : "text"}
          autoComplete="off"
          className="input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={prompt.expects === "confirm" ? "yes" : "Your response"}
          disabled={respond.isPending}
        />
        <button
          type="button"
          className="btn-primary"
          onClick={() => respond.mutate()}
          disabled={respond.isPending || !value}
        >
          <Send size={15} />
        </button>
      </div>
      {respond.error ? (
        <div className="mt-2">
          <ErrorBanner error={respond.error} />
        </div>
      ) : null}
    </div>
  );
}
