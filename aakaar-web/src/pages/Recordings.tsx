// Activity recordings (tenant-admin only).
//
// Start a desktop capture on an online agent, watch the live event count,
// then stop to compile the capture into a draft workflow — or discard it.
// Recording state lives in server memory: a server restart forgets in-flight
// recordings, and stop/discard remove the entry entirely.

import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CircleDot,
  MonitorSmartphone,
  ShieldCheck,
  Square,
  Trash2,
} from "lucide-react";

import { agents as agentsApi, recordings as recordingsApi } from "@/api";
import type {
  RecordingListItem,
  RecordingStartResponse,
  RecordingStopResponse,
} from "@/api/types";
import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDateTime, formatISTTime } from "@/lib/datetime";

// Agents must advertise this capability (and be online) to host a recording.
const RECORDING_CAP = "cap.activity_recording";

export function RecordingsPage() {
  const queryClient = useQueryClient();

  const agentsQ = useQuery({
    queryKey: ["agents"],
    queryFn: agentsApi.list,
    // The form filters on `online` — keep that reasonably fresh.
    refetchInterval: 10_000,
  });
  const listQ = useQuery({
    queryKey: ["recordings"],
    queryFn: recordingsApi.list,
    // Cheap in-memory list; keep it fresh so stop/expiry elsewhere shows up.
    refetchInterval: 5_000,
  });

  const [name, setName] = useState("");
  const [agentAlias, setAgentAlias] = useState("");
  // The latest start response carries the server-authored privacy note —
  // keep it visible while anything is recording.
  const [started, setStarted] = useState<RecordingStartResponse | null>(null);
  const [result, setResult] = useState<RecordingStopResponse | null>(null);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["recordings"] });

  const start = useMutation({
    mutationFn: () =>
      recordingsApi.start({ name: name.trim(), agent_alias: agentAlias }),
    onSuccess: (res) => {
      setStarted(res);
      setResult(null);
      setName("");
      invalidate();
    },
  });

  // Stop/discard live at page level (keyed by recording_id via `variables`)
  // so their errors survive the card disappearing — a stop that fails the
  // privacy contract or finds no compilable events REMOVES the server entry,
  // and the user still needs to read why.
  const stop = useMutation({
    mutationFn: (recordingId: string) => recordingsApi.stop(recordingId),
    onSuccess: (res) => {
      setResult(res);
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onSettled: invalidate,
  });
  const discard = useMutation({
    mutationFn: (recordingId: string) => recordingsApi.discard(recordingId),
    onSettled: invalidate,
  });

  const capableAgents = (agentsQ.data ?? []).filter(
    (a) => a.online && a.capabilities.some((c) => c.ref === RECORDING_CAP),
  );
  const items = listQ.data ?? [];

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !agentAlias) return;
    start.mutate();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Activity recordings"
        subtitle="Record desktop activity on an enrolled agent and compile it into a draft workflow. Keystrokes are redacted on the agent — review the draft before running it."
      />

      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        <div className="grid grid-cols-3 gap-6">
          {/* ---------- start form ---------- */}
          <section>
            <h3 className="panel-title mb-3 flex items-center gap-1.5">
              <CircleDot size={12} />
              New recording
            </h3>
            <form onSubmit={onSubmit} className="card space-y-3 p-4">
              <label className="block">
                <span className="panel-title">Name</span>
                <input
                  type="text"
                  className="input mt-1"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Invoice entry walkthrough"
                  maxLength={255}
                  required
                />
                <span className="mt-1 block text-[11px] text-ink-500">
                  Becomes the draft workflow's name when you stop.
                </span>
              </label>

              <label className="block">
                <span className="panel-title flex items-center gap-1.5">
                  <MonitorSmartphone size={11} />
                  Agent
                </span>
                <select
                  className="input mt-1 text-xs"
                  value={agentAlias}
                  onChange={(e) => setAgentAlias(e.target.value)}
                  required
                >
                  <option value="">Choose an online agent…</option>
                  {capableAgents.map((a) => (
                    <option key={a.id} value={a.alias}>
                      {a.alias}
                      {a.hostname ? ` — ${a.hostname}` : ""}
                    </option>
                  ))}
                </select>
                {!agentsQ.isLoading && capableAgents.length === 0 ? (
                  <span className="mt-1 block text-[11px] text-amber-300">
                    No online agent advertises {RECORDING_CAP}. Enroll one (with
                    the recording extra installed) under{" "}
                    <Link to="/agents" className="underline">
                      Agents
                    </Link>
                    .
                  </span>
                ) : null}
              </label>

              {start.error ? <ErrorBanner error={start.error} /> : null}

              <button
                type="submit"
                className="btn-primary w-full"
                disabled={start.isPending || !name.trim() || !agentAlias}
              >
                <CircleDot size={14} />
                {start.isPending ? "Starting…" : "Start recording"}
              </button>
            </form>

            {started ? (
              <div className="mt-4 flex items-start gap-2 rounded-control border border-ink-700 bg-ink-950/60 px-3 py-2.5 text-xs leading-5 text-ink-300">
                <ShieldCheck size={14} className="mt-0.5 shrink-0 text-emerald-300" />
                <span>{started.privacy_note}</span>
              </div>
            ) : null}
          </section>

          {/* ---------- active recordings + last result ---------- */}
          <section className="col-span-2">
            <h3 className="panel-title mb-3">Active recordings</h3>

            {stop.error ? (
              <div className="mb-3">
                <ErrorBanner error={describeStopError(stop.error)} />
              </div>
            ) : null}
            {discard.error ? (
              <div className="mb-3">
                <ErrorBanner error={discard.error} />
              </div>
            ) : null}

            {listQ.isLoading ? (
              <div className="text-sm text-ink-400">Loading…</div>
            ) : listQ.error ? (
              <ErrorBanner error={listQ.error} />
            ) : items.length === 0 ? (
              result ? null : (
                <EmptyState
                  title="Nothing recording"
                  description="Start a capture on an online agent to turn real desktop activity into a draft workflow."
                />
              )
            ) : (
              <ul className="space-y-3">
                {items.map((item) => (
                  <RecordingCard
                    key={item.recording_id}
                    item={item}
                    stopping={
                      stop.isPending && stop.variables === item.recording_id
                    }
                    discarding={
                      discard.isPending &&
                      discard.variables === item.recording_id
                    }
                    busy={stop.isPending || discard.isPending}
                    onStop={() => {
                      stop.reset();
                      discard.reset();
                      stop.mutate(item.recording_id);
                    }}
                    onDiscard={() => {
                      if (
                        window.confirm(
                          "Discard this recording? Captured events are dropped and nothing is saved.",
                        )
                      ) {
                        stop.reset();
                        discard.reset();
                        discard.mutate(item.recording_id);
                      }
                    }}
                  />
                ))}
              </ul>
            )}

            {result ? <StopResultPanel result={result} /> : null}
          </section>
        </div>
      </div>
    </div>
  );
}

// A stop that violates the privacy contract or finds no compilable events
// removes the server entry — make sure the user knows the capture is gone.
function describeStopError(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = err.detail || err.message;
    if (detail.includes("privacy contract")) {
      return `${detail} — nothing was saved and the recording was removed.`;
    }
    if (err.status === 422) {
      return `${detail} — the recording was removed and no workflow was created.`;
    }
    return detail;
  }
  return err instanceof Error ? err.message : String(err);
}

// ---------- live recording card ---------------------------------------------

function RecordingCard({
  item,
  stopping,
  discarding,
  busy,
  onStop,
  onDiscard,
}: {
  item: RecordingListItem;
  stopping: boolean;
  discarding: boolean;
  busy: boolean;
  onStop: () => void;
  onDiscard: () => void;
}) {
  // Live poll round-trips to the agent for the event count. 502 means the
  // agent is unreachable right now — the entry survives, and discard still
  // works, so we show a warning instead of failing the card. Polling stops
  // while a stop is compiling (it can take a while and hits the same agent).
  const statusQ = useQuery({
    queryKey: ["recording", item.recording_id],
    queryFn: () => recordingsApi.status(item.recording_id),
    refetchInterval: stopping ? false : 2_000,
    enabled: !stopping,
    retry: false,
  });
  const status = statusQ.data;
  const unreachable =
    statusQ.error instanceof ApiError && statusQ.error.status === 502;

  return (
    <li className="card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <CircleDot size={14} className="animate-pulse text-rose-400" />
            <span className="truncate text-sm font-semibold text-ink-50">
              {item.name}
            </span>
            <span className="inline-flex items-center gap-1 rounded bg-signal-cyan/15 px-1.5 py-0.5 font-mono text-[10px] text-signal-cyan">
              <MonitorSmartphone size={10} />
              {item.agent_alias}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-500">
            <span>started {formatISTDateTime(item.started_at)}</span>
            <span>expires {formatISTTime(item.expires_at)} IST</span>
            {status ? (
              <>
                <span className="font-mono text-ink-200">
                  {status.event_count} events
                </span>
                <span>{Math.round(status.duration_seconds)}s</span>
              </>
            ) : null}
          </div>
          {unreachable ? (
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-amber-300">
              <AlertTriangle size={12} className="shrink-0" />
              Agent unreachable right now — you can retry stop, or discard the
              recording.
            </div>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            className="btn-primary !min-h-0 !px-3 !py-1.5 text-xs"
            onClick={onStop}
            disabled={busy}
            title="Stop and compile the capture into a draft workflow"
          >
            <Square size={13} />
            {stopping ? "Compiling…" : "Stop & compile"}
          </button>
          <button
            type="button"
            className="btn-ghost !min-h-0 !px-2.5 !py-1.5 text-xs text-rose-300 hover:bg-rose-500/10"
            onClick={onDiscard}
            disabled={busy}
            title="Drop the capture without saving anything"
          >
            <Trash2 size={13} />
            {discarding ? "Discarding…" : "Discard"}
          </button>
        </div>
      </div>
    </li>
  );
}

// ---------- stop result ------------------------------------------------------

function StopResultPanel({ result }: { result: RecordingStopResponse }) {
  return (
    <div className="card mt-4 space-y-3 border-emerald-400/30 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-emerald-300">
            Draft workflow created
          </div>
          <div className="mt-0.5 text-xs text-ink-300">
            “{result.workflow_name}” — {result.event_count} events compiled into{" "}
            {result.draft_dag.nodes.length} steps.
          </div>
        </div>
        <Link
          to={`/workflows/${result.workflow_id}`}
          className="btn-primary shrink-0 !min-h-0 !px-3 !py-1.5 text-xs"
        >
          Open draft
          <ArrowRight size={13} />
        </Link>
      </div>

      {result.warnings.length > 0 ? (
        <div className="rounded-control border border-amber-300/35 bg-amber-950/40 px-3 py-2.5 text-amber-100">
          <div className="flex items-center gap-1.5 text-xs font-semibold">
            <AlertTriangle size={13} />
            Review before running
          </div>
          <ul className="mt-1.5 list-disc space-y-1 pl-5 text-xs leading-5 text-amber-200/90">
            {result.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {result.rationale ? (
        <p className="text-[11px] leading-5 text-ink-500">{result.rationale}</p>
      ) : null}
    </div>
  );
}
