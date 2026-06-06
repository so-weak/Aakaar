import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import {
  CheckCircle2,
  Circle,
  Download,
  ExternalLink,
  FileText,
  GitBranch,
  ListOrdered,
  MonitorSmartphone,
  Pause,
  PlayCircle,
  Send,
  X,
  XCircle,
} from "lucide-react";

import { runs as runsApi, superuser as superuserApi, workflows as workflowsApi } from "@/api";
import type { PendingPrompt, RunDetail, RunEvent } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBanner } from "@/components/ErrorBanner";
import { LiveDagViewer, deriveNodeAgents } from "@/components/LiveDagViewer";
import { LiveScreenPanel } from "@/components/LiveScreenPanel";
import { PageHeader } from "@/components/PageHeader";
import { useRunEvents } from "@/hooks/useRunEvents";
import { useLabels, useRunStatusLabel } from "@/i18n/LanguageProvider";
import { formatISTDateTime, formatISTTime } from "@/lib/datetime";
import { useObjectBlob } from "@/lib/objectBlob";

/**
 * Merge polled events with live WebSocket events into a single list, deduped
 * by `sequence` and ordered ascending. Polled events win on a sequence
 * collision (they carry the server-canonical payload). WS payloads are
 * normalized to a plain object to match the api RunEvent shape.
 */
function mergeRunEvents(
  polled: RunEvent[],
  live: { sequence: number; node_id: string | null; kind: string; payload: unknown; at: string }[],
): RunEvent[] {
  const bySeq = new Map<number, RunEvent>();
  for (const e of live) {
    bySeq.set(e.sequence, {
      sequence: e.sequence,
      node_id: e.node_id,
      kind: e.kind,
      payload:
        e.payload && typeof e.payload === "object"
          ? (e.payload as Record<string, unknown>)
          : {},
      at: e.at,
    });
  }
  // Poll results overwrite live entries with the canonical server shape.
  for (const e of polled) bySeq.set(e.sequence, e);
  return Array.from(bySeq.values()).sort((a, b) => a.sequence - b.sequence);
}

export function RunDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [view, setView] = useState<"graph" | "timeline">("graph");
  const { claims, token } = useAuth();
  const labels = useLabels();
  const isSuper = claims?.role === "superuser";

  const { data, isLoading, error } = useQuery<RunDetail>({
    // Tenant-scoped /runs/{id} rejects superusers; route them through
    // the cross-tenant /superuser/runs/{id} variant.
    queryKey: [isSuper ? "su-run" : "run", id],
    queryFn: () => (isSuper ? superuserApi.getRunDetail(id) : runsApi.get(id)),
    refetchInterval: (q) => {
      const r = q.state.data?.run;
      if (!r) return 1_500;
      if (r.status === "succeeded" || r.status === "failed" || r.status === "cancelled")
        return false;
      return 1_500;
    },
    enabled: !!id,
  });

  // Live event stream over WebSocket — augments the 2s poll above with
  // instant updates. The poll stays as a fallback (and keeps run status /
  // outputs / prompts fresh). The WS is tenant-scoped, so superusers (who
  // read runs through the cross-tenant REST variant) skip it; finished runs
  // stay inert too. A null runId/token makes the hook a no-op.
  const runStatus = data?.run.status;
  const isTerminal =
    runStatus === "succeeded" ||
    runStatus === "failed" ||
    runStatus === "cancelled";
  const liveRunId = !isSuper && !!data && !isTerminal ? id : null;
  const { events: liveEvents } = useRunEvents(liveRunId, token);

  // The DAG is immutable per (workflow_id, version) — fetch once and cache.
  const versionQ = useQuery({
    queryKey: [
      isSuper ? "su-workflow-version" : "workflow-version",
      data?.run.workflow_id,
      data?.run.workflow_version,
    ],
    queryFn: () =>
      isSuper
        ? superuserApi.getWorkflowVersion(
            data!.run.workflow_id,
            data!.run.workflow_version,
          )
        : workflowsApi.getVersion(data!.run.workflow_id, data!.run.workflow_version),
    enabled: !!data?.run.workflow_id,
    staleTime: Infinity,
  });

  // Merge polled events with live WS events, deduped by sequence and ordered.
  // Hooks must run before any early return, so we compute on (data?.events).
  const polledEvents = data?.events;
  const events = useMemo(
    () => mergeRunEvents(polledEvents ?? [], liveEvents),
    [polledEvents, liveEvents],
  );
  // Per-node run provenance (which remote agent ran each node), derived from
  // the same merged event stream so the timeline can badge it.
  const agentsByNode = useMemo(() => deriveNodeAgents(events), [events]);

  if (isLoading) return <div className="p-7 text-sm text-ink-400">Loading…</div>;
  if (error)
    return (
      <div className="p-7">
        <ErrorBanner error={error} />
      </div>
    );
  if (!data) return null;

  const { run, pending_prompts } = data;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={`${labels.yajna} ${run.id.slice(0, 8)}`}
        subtitle={
          <>
            {labels.sutra}{" "}
            <span className="font-mono text-ink-300">
              {run.workflow_id.slice(0, 8)}
            </span>{" "}
            · v{run.workflow_version} · started {formatISTDateTime(run.started_at)}
          </>
        }
        actions={
          <div className="flex items-center gap-3">
            <ViewToggle view={view} onChange={setView} />
            <StatusPill status={run.status} />
          </div>
        }
      />

      <div className="relative z-10 grid min-h-0 flex-1 grid-cols-3 gap-0 overflow-hidden">
        <section className="col-span-2 flex min-h-0 flex-col overflow-hidden border-r border-ink-700/80">
          {view === "graph" ? (
            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <div className="flex min-h-0 flex-[3] flex-col overflow-hidden">
                <div className="border-b border-ink-700/80 bg-ink-950/45 px-6 py-3 panel-title">
                  Graph view · live step indicator
                </div>
                <div className="min-h-0 flex-1 overflow-hidden">
                  {versionQ.data ? (
                    <LiveDagViewer
                      dag={versionQ.data.dag}
                      events={events}
                      runStatus={run.status}
                    />
                  ) : versionQ.error ? (
                    <div className="p-6">
                      <ErrorBanner error={versionQ.error} />
                    </div>
                  ) : (
                    <div className="p-6 text-sm text-ink-400">Loading DAG…</div>
                  )}
                </div>
              </div>
              <div className="flex min-h-0 flex-[2] flex-col overflow-hidden border-t border-ink-700/80">
                <LiveScreenPanel events={events} />
              </div>
            </div>
          ) : (
            <>
              <div className="border-b border-ink-700/80 bg-ink-950/45 px-6 py-3 panel-title">
                Timeline · detailed event log
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
                {events.length === 0 ? (
                  <div className="text-sm text-ink-400">No events yet.</div>
                ) : (
                  <ol className="space-y-2">
                    {events.map((e) => (
                      <EventRow
                        key={e.sequence}
                        event={e}
                        agent={e.node_id ? agentsByNode[e.node_id] ?? null : null}
                      />
                    ))}
                  </ol>
                )}
              </div>
            </>
          )}
        </section>

        <aside className="flex min-h-0 flex-col overflow-y-auto bg-ink-950/45 px-5 py-5 backdrop-blur">
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

          <MediaPanel outputs={run.outputs} />

          <section className="mt-5">
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

function ViewToggle({
  view,
  onChange,
}: {
  view: "graph" | "timeline";
  onChange: (v: "graph" | "timeline") => void;
}) {
  return (
    <div className="inline-flex rounded-md border border-ink-700 bg-ink-950/60 p-0.5">
      <button
        type="button"
        className={[
          "flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-semibold uppercase tracking-wider transition",
          view === "graph"
            ? "bg-accent-300/15 text-accent-100"
            : "text-ink-400 hover:text-ink-200",
        ].join(" ")}
        onClick={() => onChange("graph")}
      >
        <GitBranch size={12} /> Graph
      </button>
      <button
        type="button"
        className={[
          "flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-semibold uppercase tracking-wider transition",
          view === "timeline"
            ? "bg-accent-300/15 text-accent-100"
            : "text-ink-400 hover:text-ink-200",
        ].join(" ")}
        onClick={() => onChange("timeline")}
      >
        <ListOrdered size={12} /> Timeline
      </button>
    </div>
  );
}

function StatusPill({ status }: { status: RunDetail["run"]["status"] }) {
  const runStatusLabel = useRunStatusLabel();
  const map = {
    queued: "ring-ink-700 text-ink-300",
    running: "ring-accent-500/40 text-accent-300",
    paused: "ring-amber-400/40 text-amber-300",
    succeeded: "ring-emerald-400/40 text-emerald-300",
    failed: "ring-rose-400/40 text-rose-300",
    cancelled: "ring-ink-700 text-ink-400",
  } as const;
  return <span className={`badge ${map[status]}`}>{runStatusLabel(status)}</span>;
}

function EventRow({ event, agent }: { event: RunEvent; agent: string | null }) {
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
          <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
            <span className="text-ink-200">{event.kind}</span>
            {event.node_id ? (
              <>
                <span>·</span>
                <span className="font-mono">{event.node_id}</span>
              </>
            ) : null}
            {agent ? (
              <span
                className="inline-flex items-center gap-1 rounded bg-signal-cyan/15 px-1.5 py-0.5 font-mono text-[10px] text-signal-cyan"
                title={`Ran on ${agent}`}
              >
                <MonitorSmartphone size={10} className="shrink-0" />
                ran on {agent}
              </span>
            ) : null}
          </span>
          <time>{formatISTTime(event.at)} IST</time>
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

// ---------- managed-storage media -----------------------------------------

const _AAKAAR_URI_RE = /aakaar:\/\/[\w./-]+/g;
const _IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|svg)(\?|$)/i;

function _extractAakaarUris(text: string): string[] {
  return Array.from(text.matchAll(_AAKAAR_URI_RE), (m) => m[0]);
}

function _filenameFromUri(uri: string): string {
  const tail = uri.split("/").pop() ?? "download.bin";
  // Strip our run-prefix uuid like "<uuid>_real-name.csv" → "real-name.csv".
  const m = tail.match(/^[0-9a-f]{32}_(.+)$/i);
  return m ? m[1] : tail;
}

function _isImageUri(uri: string): boolean {
  return _IMAGE_EXT_RE.test(uri);
}

function _triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke on the next tick — Safari needs the URL to remain valid through
  // the click handler.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Small thumbnail or "download me" tile, depending on whether the URI is an image. */
function MediaThumbnail({
  uri,
  label,
  size = "sm",
}: {
  uri: string;
  label?: string;
  size?: "sm" | "md";
}) {
  const [enlarged, setEnlarged] = useState(false);
  const { src, blob, err } = useObjectBlob(uri);
  const filename = _filenameFromUri(uri);
  const isImage = _isImageUri(uri);

  const onDownload = useCallback(() => {
    if (blob) _triggerDownload(blob, filename);
  }, [blob, filename]);

  if (err) {
    return (
      <div className="rounded border border-rose-500/40 bg-rose-500/5 px-2 py-1.5 text-xs text-rose-300">
        Couldn’t load {filename}: {err}
      </div>
    );
  }

  const wrapperClass =
    size === "md"
      ? "rounded border border-ink-700 bg-ink-900/60 p-2"
      : "rounded border border-ink-700 bg-ink-900/60 p-1.5";

  if (!isImage) {
    return (
      <div className={`${wrapperClass} flex items-center gap-2`}>
        <FileText size={16} className="shrink-0 text-ink-400" />
        <div className="flex-1 truncate">
          {label ? (
            <div className="text-[11px] uppercase tracking-wider text-ink-500">{label}</div>
          ) : null}
          <div className="truncate text-xs text-ink-200">{filename}</div>
        </div>
        <button
          type="button"
          className="btn-ghost shrink-0"
          onClick={onDownload}
          disabled={!blob}
          title="Download"
        >
          <Download size={14} />
        </button>
      </div>
    );
  }

  return (
    <>
      <div className={wrapperClass}>
        {label ? (
          <div className="mb-1 text-[11px] uppercase tracking-wider text-ink-500">
            {label}
          </div>
        ) : null}
        <button
          type="button"
          className="block w-full overflow-hidden rounded border border-ink-700 bg-white/95 transition hover:border-accent-300/60"
          onClick={() => setEnlarged(true)}
          title="Click to enlarge"
        >
          {src ? (
            <img
              src={src}
              alt={filename}
              className={size === "md" ? "max-h-48 w-full object-contain" : "max-h-32 w-full object-contain"}
            />
          ) : (
            <div className="grid h-24 place-items-center text-xs text-ink-500">Loading…</div>
          )}
        </button>
        <div className="mt-1.5 flex items-center justify-between gap-2 text-[11px] text-ink-500">
          <span className="truncate font-mono" title={filename}>
            {filename}
          </span>
          <span className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              className="btn-ghost px-1.5 py-0.5"
              onClick={() => setEnlarged(true)}
              disabled={!src}
              title="Enlarge"
            >
              <ExternalLink size={12} />
            </button>
            <button
              type="button"
              className="btn-ghost px-1.5 py-0.5"
              onClick={onDownload}
              disabled={!blob}
              title="Download"
            >
              <Download size={12} />
            </button>
          </span>
        </div>
      </div>
      {enlarged && src ? (
        <Lightbox
          src={src}
          filename={filename}
          onDownload={onDownload}
          canDownload={!!blob}
          onClose={() => setEnlarged(false)}
        />
      ) : null}
    </>
  );
}

function Lightbox({
  src,
  filename,
  onDownload,
  canDownload,
  onClose,
}: {
  src: string;
  filename: string;
  onDownload: () => void;
  canDownload: boolean;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[60] flex flex-col bg-ink-950/90 backdrop-blur"
      onClick={onClose}
    >
      <div
        className="flex items-center justify-between gap-3 border-b border-ink-700/80 bg-ink-950/80 px-5 py-3 text-xs text-ink-300"
        onClick={(e) => e.stopPropagation()}
      >
        <span className="truncate font-mono">{filename}</span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn-ghost"
            onClick={onDownload}
            disabled={!canDownload}
          >
            <Download size={14} /> Download
          </button>
          <button type="button" className="btn-ghost" onClick={onClose}>
            <X size={14} /> Close
          </button>
        </div>
      </div>
      <div className="flex flex-1 items-center justify-center overflow-auto p-6">
        <img
          src={src}
          alt={filename}
          className="max-h-full max-w-full rounded border border-ink-700 bg-white"
          onClick={(e) => e.stopPropagation()}
        />
      </div>
    </div>
  );
}

/**
 * Walk a run's nested outputs and surface every aakaar:// URI we find with a
 * `node.field` label so the panel can show it inline.
 */
function _scanOutputsForUris(
  outputs: Record<string, Record<string, unknown>>,
): { node: string; field: string; uri: string }[] {
  const out: { node: string; field: string; uri: string }[] = [];
  for (const [node, fields] of Object.entries(outputs ?? {})) {
    if (!fields || typeof fields !== "object") continue;
    for (const [field, value] of Object.entries(fields)) {
      if (typeof value === "string" && value.startsWith("aakaar://")) {
        out.push({ node, field, uri: value });
      }
    }
  }
  return out;
}

function MediaPanel({
  outputs,
}: {
  outputs: Record<string, Record<string, unknown>>;
}) {
  const items = useMemo(() => _scanOutputsForUris(outputs), [outputs]);
  if (items.length === 0) return null;
  return (
    <section className="mb-5">
      <h3 className="panel-title mb-2">
        Media <span className="ml-1 text-ink-600">({items.length})</span>
      </h3>
      <div className="grid grid-cols-1 gap-2">
        {items.map((it) => (
          <MediaThumbnail
            key={it.uri}
            uri={it.uri}
            label={`${it.node}.${it.field}`}
            size="md"
          />
        ))}
      </div>
    </section>
  );
}

// ---------- prompts ------------------------------------------------------

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

  const imageUris = _extractAakaarUris(prompt.message).filter(_isImageUri);

  return (
    <div className="card p-3">
      <div className="mb-2 flex items-center justify-between text-xs">
        <span className="font-mono text-ink-400">{prompt.node_id}</span>
        <span className="badge ring-amber-400/40 text-amber-300">{prompt.expects}</span>
      </div>
      <p className="mb-2 text-sm text-ink-100">{prompt.message}</p>
      {imageUris.length > 0 ? (
        <div className="mb-2 space-y-2">
          {imageUris.map((u) => (
            <MediaThumbnail key={u} uri={u} size="sm" />
          ))}
        </div>
      ) : null}
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
