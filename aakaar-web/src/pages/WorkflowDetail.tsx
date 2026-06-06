import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  CalendarClock,
  MessageSquarePlus,
  MonitorSmartphone,
  Pencil,
  Play,
  Plus,
  Power,
  Repeat,
  Server,
  Trash2,
  X,
} from "lucide-react";

import {
  agents as agentsApi,
  capabilities as capabilitiesApi,
  chatSessions as sessionsApi,
  placement as placementApi,
  runs as runsApi,
  schedules as schedulesApi,
  workflows as workflowsApi,
} from "@/api";
import { ApiError } from "@/api/client";
import type { Dag, PlacementIssue, RemoteAgent, WorkflowSchedule } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { DagEditor } from "@/components/DagEditor";
import type { AvailableRef } from "@/components/DagEditor";
import { DagViewer } from "@/components/DagViewer";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { useLabels } from "@/i18n/LanguageProvider";
import { formatISTDateTime } from "@/lib/datetime";

// "server"/null both mean "run on the API host"; this sentinel is the value
// the run-level selector uses for the server option.
const SERVER_TARGET = "server";

// Online agent aliases + their distinct pools, used to populate the run-level
// "Run on" selector. Offline agents are ignored — they can't pick up work.
function placementTargetsOf(agents: RemoteAgent[] | undefined): {
  aliases: string[];
  pools: string[];
} {
  const aliases: string[] = [];
  const pools = new Set<string>();
  for (const a of agents ?? []) {
    if (!a.online) continue;
    aliases.push(a.alias);
    for (const p of a.pools) pools.add(p);
  }
  return { aliases, pools: Array.from(pools).sort() };
}

// Shallow-copy the dag with every non-control node's target overridden to the
// chosen run-level target. Control nodes always stay on the server. Used to
// pre-flight a run-level placement override against /placement/check.
function dagWithTargetOverride(dag: Dag, target: string): Dag {
  return {
    ...dag,
    nodes: dag.nodes.map((n) =>
      n.kind === "control" ? { ...n, target: null } : { ...n, target },
    ),
  };
}

export function WorkflowDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { claims } = useAuth();
  const labels = useLabels();

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
  // Registry refs feed the DAG editor palette + ref validation.
  const capabilitiesQ = useQuery({
    queryKey: ["capabilities"],
    queryFn: capabilitiesApi.list,
  });
  // Online agents + their pools feed the run-level "Run on" selector.
  const agentsQ = useQuery({
    queryKey: ["agents"],
    queryFn: agentsApi.list,
    staleTime: 30_000,
  });

  // Run-level placement override chosen in the launch modal. null = use each
  // node's own placement (today's default). "server" = whole run on the API
  // host. Any other value = whole run on that agent alias / pool.
  const [launchTarget, setLaunchTarget] = useState<string | null>(null);
  const [launchOpen, setLaunchOpen] = useState(false);

  const start = useMutation({
    mutationFn: (target: string | null) => runsApi.start(id, {}, target),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/runs/${run.id}`);
    },
  });

  // Placement issues found by the pre-launch check. When non-empty we block
  // the run and surface a clear warning instead.
  const [placementIssues, setPlacementIssues] = useState<PlacementIssue[]>([]);

  // Validate placement against online agents before starting the run. With a
  // null target we check the dag as-authored (per-node placement); with a
  // non-null target we check a copy where every non-control node is forced to
  // that target. A clean result starts the run with the chosen target; issues
  // populate the warning banner. A failed check (network etc.) falls through
  // to the normal launch so remote-execution outages don't block ordinary,
  // server-only workflows.
  const launch = useMutation({
    mutationFn: ({ dag, target }: { dag: Dag; target: string | null }) =>
      placementApi.check(target === null ? dag : dagWithTargetOverride(dag, target)),
    onSuccess: (res, vars) => {
      setPlacementIssues(res.issues);
      if (res.issues.length === 0) start.mutate(vars.target);
    },
    onError: (_err, vars) => {
      setPlacementIssues([]);
      start.mutate(vars.target);
    },
  });

  const onLaunch = (dag: Dag, target: string | null) => {
    start.reset();
    setPlacementIssues([]);
    launch.mutate({ dag, target });
  };
  const launching = launch.isPending || start.isPending;

  const remove = useMutation({
    mutationFn: () => workflowsApi.remove(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate("/workflows");
    },
  });

  // Persist an edited DAG as a new workflow version via the existing update
  // endpoint (PATCH /workflows/{id}); it returns the freshly-created version.
  const saveDag = useMutation({
    mutationFn: (dag: Dag) =>
      workflowsApi.update(id, { dag, rationale: "Edited in DAG editor" }),
    onSuccess: () => {
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      queryClient.invalidateQueries({ queryKey: ["workflow", id, "latest"] });
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  // Refine: spin up a fresh chat session and jump to it. chatSessions.create
  // only accepts { title } today, so we do not pass a workflow_id (the backend
  // is unchanged); the title references this workflow for context.
  const refine = useMutation({
    mutationFn: () =>
      sessionsApi.create({ title: `Refine: ${workflowQ.data?.name ?? "workflow"}` }),
    onSuccess: (session) => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      navigate(`/chat/${session.id}`);
    },
  });

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [editing, setEditing] = useState(false);

  const availableRefs = useMemo<AvailableRef[]>(
    () =>
      (capabilitiesQ.data ?? []).map((c) => ({
        ref: c.ref,
        kind: c.kind,
        description: c.description,
      })),
    [capabilitiesQ.data],
  );

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
              className="btn-ghost"
              onClick={() => refine.mutate()}
              disabled={refine.isPending}
              title="Open a chat session to refine this workflow"
            >
              <MessageSquarePlus size={15} />
              {refine.isPending ? "Opening…" : "Refine"}
            </button>
            <button
              type="button"
              className="btn-ghost"
              onClick={() => setEditing(true)}
              title="Edit the DAG and save a new version"
            >
              <Pencil size={15} />
              Edit DAG
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                start.reset();
                launch.reset();
                setPlacementIssues([]);
                setLaunchTarget(null);
                setLaunchOpen(true);
              }}
              disabled={launching}
            >
              <Play size={15} /> {labels.runYajna}
            </button>
          </>
        }
      />

      {refine.error ? (
        <div className="border-b border-ink-800 p-3">
          <ErrorBanner error={refine.error} />
        </div>
      ) : null}

      <div className="relative z-10 grid min-h-0 flex-1 grid-cols-3 overflow-hidden">
        <div className="col-span-2 min-h-0 overflow-hidden border-r border-ink-700/80">
          <DagViewer dag={version.dag} />
        </div>
        <aside className="min-h-0 overflow-y-auto bg-ink-950/45 px-5 py-5 backdrop-blur">
          <SchedulesPanel workflowId={id} agents={agentsQ.data} />
        </aside>
      </div>

      {launchOpen ? (
        <LaunchModal
          target={launchTarget}
          onTargetChange={(t) => {
            // Changing the target invalidates any prior pre-flight result; the
            // user re-runs the check by hitting Run again.
            setLaunchTarget(t);
            setPlacementIssues([]);
            launch.reset();
            start.reset();
          }}
          agents={agentsQ.data}
          issues={placementIssues}
          checking={launch.isPending}
          starting={start.isPending}
          error={launch.error ?? (start.error ? describeStartError(start.error) : null)}
          onRun={() => onLaunch(version.dag, launchTarget)}
          onRunAnyway={() => {
            setPlacementIssues([]);
            start.mutate(launchTarget);
          }}
          onClose={() => {
            if (!launching) setLaunchOpen(false);
          }}
        />
      ) : null}

      {editing ? (
        <DagEditorModal
          dag={version.dag}
          availableRefs={availableRefs}
          saving={saveDag.isPending}
          error={saveDag.error}
          onSave={(dag) => saveDag.mutate(dag)}
          onCancel={() => {
            if (!saveDag.isPending) {
              saveDag.reset();
              setEditing(false);
            }
          }}
        />
      ) : null}
    </div>
  );
}

// ---------- placement warning ---------------------------------------------

function PlacementWarning({
  issues,
  onRunAnyway,
  onDismiss,
  disabled,
}: {
  issues: PlacementIssue[];
  onRunAnyway: () => void;
  onDismiss?: () => void;
  disabled: boolean;
}) {
  return (
    <div className="brand-shadow-pink-sm rounded-control border border-amber-300/35 bg-amber-950/40 px-4 py-3 text-amber-100">
      <div className="flex items-start gap-2">
        <AlertTriangle size={16} className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold">
            Run blocked — {issues.length} node{issues.length === 1 ? "" : "s"} can’t
            be placed
          </div>
          <p className="mt-0.5 text-xs text-amber-200/90">
            These nodes target a remote agent or pool that isn’t available right
            now. Enroll/bring an agent online, change the target in the DAG editor,
            or run anyway (those nodes will fail).
          </p>
          <ul className="mt-2 space-y-1.5">
            {issues.map((it) => (
              <li
                key={it.node_id}
                className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs"
              >
                <span className="font-mono text-amber-100">{it.node_id}</span>
                <span className="inline-flex items-center gap-1 rounded bg-amber-300/15 px-1.5 py-0.5 font-mono text-[11px]">
                  <MonitorSmartphone size={11} />
                  {it.target}
                </span>
                <span className="text-amber-200/90">{it.reason}</span>
              </li>
            ))}
          </ul>
          <div className="mt-3 flex items-center gap-2">
            {onDismiss ? (
              <button
                type="button"
                className="btn-ghost !min-h-0 !px-2.5 !py-1.5 text-xs"
                onClick={onDismiss}
                disabled={disabled}
              >
                <X size={13} />
                Dismiss
              </button>
            ) : null}
            <button
              type="button"
              className="btn-ghost !min-h-0 !px-2.5 !py-1.5 text-xs text-amber-200 hover:bg-amber-500/10"
              onClick={onRunAnyway}
              disabled={disabled}
            >
              <Play size={13} />
              Run anyway
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------- run-level "Run on" selector ------------------------------------

// A self-contained dropdown for the run-level placement override, shared by
// the launch modal and the schedule form. `value`/`onChange` use null for the
// "each step's setting" default and a string for any concrete target.
function RunOnSelect({
  value,
  onChange,
  agents,
  disabled,
  id,
}: {
  value: string | null;
  onChange: (target: string | null) => void;
  agents: RemoteAgent[] | undefined;
  disabled?: boolean;
  id?: string;
}) {
  const { aliases, pools } = placementTargetsOf(agents);
  // Map the option's string value back to the target the backend expects.
  // "" -> null (per-step default); SERVER_TARGET -> "server"; otherwise alias/pool.
  const selectValue = value === null ? "" : value;

  return (
    <label className="block">
      <span className="panel-title flex items-center gap-1.5">
        <MonitorSmartphone size={11} />
        Run on
      </span>
      <select
        id={id}
        className="input mt-1 text-xs"
        value={selectValue}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      >
        <option value="">Each step's setting (default)</option>
        <option value={SERVER_TARGET}>Server — run everything here</option>
        {aliases.length > 0 ? (
          <optgroup label="Agents (online)">
            {aliases.map((alias) => (
              <option key={`a-${alias}`} value={alias}>
                {alias}
              </option>
            ))}
          </optgroup>
        ) : null}
        {pools.length > 0 ? (
          <optgroup label="Pools">
            {pools.map((pool) => (
              <option key={`p-${pool}`} value={pool}>
                {pool}
              </option>
            ))}
          </optgroup>
        ) : null}
      </select>
    </label>
  );
}

// ---------- launch modal ----------------------------------------------------

function LaunchModal({
  target,
  onTargetChange,
  agents,
  issues,
  checking,
  starting,
  error,
  onRun,
  onRunAnyway,
  onClose,
}: {
  target: string | null;
  onTargetChange: (target: string | null) => void;
  agents: RemoteAgent[] | undefined;
  issues: PlacementIssue[];
  checking: boolean;
  starting: boolean;
  error: unknown;
  onRun: () => void;
  onRunAnyway: () => void;
  onClose: () => void;
}) {
  const busy = checking || starting;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 backdrop-blur">
      <div className="card w-full max-w-lg p-5">
        <div className="mb-1 flex items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-base font-semibold text-ink-50">
            <Play size={16} className="text-accent-300" />
            Launch run
          </h3>
          <button
            type="button"
            className="btn-ghost !min-h-0 !px-2 !py-1"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
          >
            <X size={15} />
          </button>
        </div>

        <p className="mb-3 text-xs text-ink-400">
          Choose where this run executes. The default honours each step's own
          placement; pick a single target to run the whole workflow there
          (control steps always stay on the server).
        </p>

        <RunOnSelect
          id="launch-run-on"
          value={target}
          onChange={onTargetChange}
          agents={agents}
          disabled={busy}
        />

        {target === SERVER_TARGET ? (
          <p className="mt-1 flex items-center gap-1 text-[11px] text-ink-500">
            <Server size={11} />
            Per-step agent targets are ignored; everything runs on the API host.
          </p>
        ) : target !== null ? (
          <p className="mt-1 flex items-center gap-1 text-[11px] text-ink-500">
            <MonitorSmartphone size={11} />
            The whole workflow runs on “{target}”. Control steps stay on the
            server.
          </p>
        ) : null}

        {issues.length > 0 ? (
          <div className="mt-4">
            <PlacementWarning
              issues={issues}
              onRunAnyway={onRunAnyway}
              disabled={busy}
            />
          </div>
        ) : null}

        {error ? (
          <div className="mt-4">
            <ErrorBanner error={error} />
          </div>
        ) : null}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            className="btn-ghost"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={onRun}
            disabled={busy}
          >
            <Play size={15} />
            {checking ? "Checking…" : starting ? "Starting…" : "Run"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------- DAG editor modal ----------------------------------------------

function DagEditorModal({
  dag,
  availableRefs,
  saving,
  error,
  onSave,
  onCancel,
}: {
  dag: Dag;
  availableRefs: AvailableRef[];
  saving: boolean;
  error: unknown;
  onSave: (dag: Dag) => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-ink-950/85 backdrop-blur">
      <div className="flex items-center justify-between gap-3 border-b border-ink-700/80 bg-ink-950/80 px-5 py-3">
        <div>
          <div className="stamp">DAG editor</div>
          <h2 className="headline mt-1 text-base text-ink-50">
            Editing — saves a new version
          </h2>
        </div>
        <button
          type="button"
          className="btn-ghost"
          onClick={onCancel}
          disabled={saving}
        >
          <X size={15} /> Close
        </button>
      </div>
      {error ? (
        <div className="border-b border-ink-700/80 px-5 py-3">
          <ErrorBanner error={error} />
        </div>
      ) : null}
      <div className="relative min-h-0 flex-1">
        <DagEditor dag={dag} availableRefs={availableRefs} onSave={onSave} onCancel={onCancel} />
        {saving ? (
          <div className="absolute inset-0 z-10 grid place-items-center bg-ink-950/60">
            <span className="text-sm text-ink-200">Saving new version…</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------- schedules panel -----------------------------------------------

type ScheduleMode = "calendar" | "cron";

function SchedulesPanel({
  workflowId,
  agents,
}: {
  workflowId: string;
  agents: RemoteAgent[] | undefined;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["schedules", workflowId];

  const schedulesQ = useQuery({
    queryKey,
    queryFn: () => schedulesApi.list(workflowId),
    enabled: !!workflowId,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey });

  const [mode, setMode] = useState<ScheduleMode>("calendar");
  const [datetime, setDatetime] = useState("");
  const [cron, setCron] = useState("");
  // Run-level placement override for scheduled runs (null = per-step default).
  const [target, setTarget] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => {
      if (mode === "calendar") {
        // datetime-local gives wall-clock in the browser's TZ; convert to a
        // UTC ISO instant for the backend. (datetime.ts ships only formatters,
        // no to-ISO helper, so we use the standard Date conversion.)
        return schedulesApi.create(workflowId, {
          scheduled_at: new Date(datetime).toISOString(),
          target,
        });
      }
      return schedulesApi.create(workflowId, { cron: cron.trim(), target });
    },
    onSuccess: () => {
      setDatetime("");
      setCron("");
      setTarget(null);
      invalidate();
    },
  });

  const toggle = useMutation({
    mutationFn: (s: WorkflowSchedule) =>
      schedulesApi.update(s.id, { enabled: !s.enabled }),
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: (scheduleId: string) => schedulesApi.remove(scheduleId),
    onSuccess: invalidate,
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (mode === "calendar" ? !datetime : !cron.trim()) return;
    create.mutate();
  };

  const items = schedulesQ.data ?? [];

  return (
    <section>
      <h3 className="panel-title mb-3 flex items-center gap-1.5">
        <CalendarClock size={12} />
        Schedules
      </h3>

      <form onSubmit={onSubmit} className="card mb-4 space-y-3 p-3">
        <div className="inline-flex rounded-md border border-ink-700 bg-ink-950/60 p-0.5">
          <button
            type="button"
            className={modeBtn(mode === "calendar")}
            onClick={() => setMode("calendar")}
          >
            <CalendarClock size={12} /> One-off
          </button>
          <button
            type="button"
            className={modeBtn(mode === "cron")}
            onClick={() => setMode("cron")}
          >
            <Repeat size={12} /> Cron
          </button>
        </div>

        {mode === "calendar" ? (
          <label className="block">
            <span className="panel-title">Run at (local time)</span>
            <input
              type="datetime-local"
              className="input mt-1"
              value={datetime}
              onChange={(e) => setDatetime(e.target.value)}
              required
            />
          </label>
        ) : (
          <label className="block">
            <span className="panel-title">Cron expression</span>
            <input
              type="text"
              className="input mt-1 font-mono text-xs"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder="0 9 * * 1-5"
              spellCheck={false}
              required
            />
            <span className="mt-1 block text-[11px] text-ink-500">
              Standard 5-field cron. Example: 0 9 * * 1-5 (weekdays at 09:00).
            </span>
          </label>
        )}

        <RunOnSelect
          value={target}
          onChange={setTarget}
          agents={agents}
          disabled={create.isPending}
        />

        {create.error ? <ErrorBanner error={create.error} /> : null}

        <button
          type="submit"
          className="btn-primary w-full"
          disabled={
            create.isPending || (mode === "calendar" ? !datetime : !cron.trim())
          }
        >
          <Plus size={14} />
          {create.isPending ? "Scheduling…" : "Add schedule"}
        </button>
      </form>

      {schedulesQ.isLoading ? (
        <div className="text-xs text-ink-500">Loading schedules…</div>
      ) : schedulesQ.error ? (
        <ErrorBanner error={schedulesQ.error} />
      ) : items.length === 0 ? (
        <div className="text-xs text-ink-500">No schedules yet.</div>
      ) : (
        <ul className="space-y-2">
          {items.map((s) => (
            <li key={s.id} className="card p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  {s.cron ? (
                    <div className="font-mono text-xs text-ink-100">{s.cron}</div>
                  ) : (
                    <div className="text-xs text-ink-100">
                      {formatISTDateTime(s.scheduled_at)}
                    </div>
                  )}
                  <div className="mt-1 flex items-center gap-2 text-[11px]">
                    <span
                      className={
                        s.enabled
                          ? "badge ring-emerald-400/40 text-emerald-300"
                          : "badge ring-ink-700 text-ink-400"
                      }
                    >
                      {s.enabled ? "enabled" : "disabled"}
                    </span>
                    <span className="text-ink-500">
                      {s.cron ? "recurring" : "one-off"}
                    </span>
                  </div>
                  {s.last_triggered_at ? (
                    <div className="mt-1 text-[11px] text-ink-500">
                      Last run {formatISTDateTime(s.last_triggered_at)}
                    </div>
                  ) : null}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    className={[
                      "btn-ghost",
                      s.enabled ? "text-amber-300 hover:bg-amber-500/10" : "text-emerald-300 hover:bg-emerald-500/10",
                    ].join(" ")}
                    onClick={() => toggle.mutate(s)}
                    disabled={toggle.isPending}
                    title={s.enabled ? "Disable" : "Enable"}
                  >
                    <Power size={14} />
                  </button>
                  <button
                    type="button"
                    className="btn-ghost text-rose-300 hover:bg-rose-500/10"
                    onClick={() => {
                      if (window.confirm("Delete this schedule?")) {
                        remove.mutate(s.id);
                      }
                    }}
                    disabled={remove.isPending}
                    title="Delete"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {toggle.error ? (
        <div className="mt-2">
          <ErrorBanner error={toggle.error} />
        </div>
      ) : null}
      {remove.error ? (
        <div className="mt-2">
          <ErrorBanner error={remove.error} />
        </div>
      ) : null}
    </section>
  );
}

function modeBtn(active: boolean): string {
  return [
    "flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-semibold uppercase tracking-wider transition",
    active ? "bg-accent-300/15 text-accent-100" : "text-ink-400 hover:text-ink-200",
  ].join(" ");
}

function describeStartError(err: unknown): string {
  if (err instanceof ApiError) return err.detail || err.message;
  return err instanceof Error ? err.message : String(err);
}
