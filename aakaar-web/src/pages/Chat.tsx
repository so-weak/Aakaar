import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  ExternalLink,
  Eye,
  FlaskConical,
  HelpCircle,
  MessageSquare,
  MonitorDot,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Play,
  Plus,
  Save,
  Send,
  ShieldAlert,
  Sparkles,
  Trash2,
  Zap,
} from "lucide-react";

import {
  capabilities as capsApi,
  chatSessions as sessionsApi,
  runs as runsApi,
} from "@/api";
import { ApiError } from "@/api/client";
import type {
  ApprovalRequest,
  ChatSession,
  ChatSessionSummary,
  Dag,
  RawChatResponse,
  RunEvent,
  RunStatus,
} from "@/api/types";
import { isApprovalPending } from "@/api/types";
import { ChatComposer } from "@/components/ChatComposer";
import { Dialog } from "@/components/Dialog";
import { DagViewer } from "@/components/DagViewer";
import { ErrorBanner } from "@/components/ErrorBanner";
import { LiveDagViewer, deriveNodeStatuses } from "@/components/LiveDagViewer";
import { LiveScreenPanel } from "@/components/LiveScreenPanel";
import { useAuth } from "@/auth/AuthContext";
import { useRunEvents } from "@/hooks/useRunEvents";
import { useLabels, useRunStatusLabel } from "@/i18n/LanguageProvider";
import { fmt, useChatStrings } from "@/i18n/chatStrings";
import type { ChatStrings } from "@/i18n/chatStrings";
import { formatISTTime } from "@/lib/datetime";
import {
  friendlyCapabilityName,
  isSideEffectingRef,
  nodeAgentTarget,
} from "@/lib/capabilityNames";

type DockTab = "plan" | "live";

const TERMINAL: RunStatus[] = ["succeeded", "failed", "cancelled"];
const isTerminalStatus = (s?: RunStatus) => !!s && TERMINAL.includes(s);

const KIND_CHIP: Record<string, string> = {
  capability: "text-emerald-300 border-emerald-300/25 bg-emerald-300/5",
  action: "text-signal-cyan border-signal-cyan/25 bg-signal-cyan/5",
  control: "text-signal-pink border-signal-pink/25 bg-signal-pink/5",
};

// ---------- shared helpers -------------------------------------------------

function isAbortError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === "AbortError") ||
    (err instanceof Error && err.name === "AbortError")
  );
}

/** Map an error to plain, actionable operator copy (localized). */
function humanizeError(error: unknown, cs: ChatStrings): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return cs.errRate;
    if (error.status === 401 || error.status === 403) return cs.errAuth;
    if (error.status >= 500) {
      const d = error.detail.toLowerCase();
      if (d.includes("planner") || d.includes("llm") || d.includes("chain"))
        return cs.errPlanner;
      return cs.errServer;
    }
    if (error.status === 422) return cs.errValidation;
  }
  if (error instanceof Error && error.message.toLowerCase().includes("network"))
    return cs.errNetwork;
  return cs.errGeneric;
}

/** Merge polled + live-WS run events, deduped by sequence and ordered. */
function mergeEventsBySeq(a: RunEvent[], b: RunEvent[]): RunEvent[] {
  const bySeq = new Map<number, RunEvent>();
  for (const e of a) bySeq.set(e.sequence, e);
  for (const e of b) bySeq.set(e.sequence, e);
  return Array.from(bySeq.values()).sort((x, y) => x.sequence - y.sequence);
}

// ===========================================================================
// Page shell
// ===========================================================================

export function ChatPage() {
  const { id: routeId } = useParams<{ id?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const labels = useLabels();
  const cs = useChatStrings();

  const sessionsQ = useQuery({
    queryKey: ["chat-sessions"],
    queryFn: sessionsApi.list,
  });

  const activeId = routeId ?? sessionsQ.data?.[0]?.id ?? null;

  const create = useMutation({
    mutationFn: (title?: string) => sessionsApi.create({ title }),
    onSuccess: (s) => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      navigate(`/chat/${s.id}`);
    },
  });

  // Auto-create a session on first visit if the user has none.
  useEffect(() => {
    if (sessionsQ.data && sessionsQ.data.length === 0 && !create.isPending) {
      create.mutate(undefined);
    }
  }, [sessionsQ.data, create]);

  return (
    <div className="flex h-full flex-col bg-ink-950/30">
      <header className="flex min-h-[72px] items-center justify-between gap-4 border-b border-ink-800/80 bg-ink-950/50 px-5 py-3 backdrop-blur-xl">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-accent-300/25 bg-accent-300/10 text-accent-200">
            <Sparkles size={17} />
          </span>
          <div className="min-w-0">
            <h1 className="text-base font-semibold text-ink-50">{labels.samvada}</h1>
            <p className="mt-0.5 hidden truncate text-xs text-ink-400 sm:block">
              {cs.headerSubtitle}
            </p>
          </div>
        </div>
        <button
          type="button"
          className="btn-primary !min-h-9 !px-3.5 text-xs"
          onClick={() => create.mutate(undefined)}
          disabled={create.isPending}
        >
          <Plus size={14} />
          <span className="hidden sm:inline">
            {cs.newLabel} {labels.samvada.toLowerCase()}
          </span>
          <span className="sm:hidden">{cs.newLabel}</span>
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-hidden">
        {activeId ? (
          <SessionWorkspace
            key={activeId}
            sessionId={activeId}
            sessions={sessionsQ.data ?? []}
            activeId={activeId}
            onPick={(id) => navigate(`/chat/${id}`)}
            onNew={() => create.mutate(undefined)}
            isCreating={create.isPending}
          />
        ) : (
          <div className="grid h-full place-items-center text-sm text-ink-500">
            {sessionsQ.isLoading ? cs.loadingChats : cs.noActiveChat}
          </div>
        )}
      </div>
    </div>
  );
}

// ===========================================================================
// Workspace: three-zone shell
// ===========================================================================

interface WorkspaceProps {
  sessionId: string;
  sessions: ChatSessionSummary[];
  activeId: string | null;
  onPick: (id: string) => void;
  onNew: () => void;
  isCreating: boolean;
}

function readBool(key: string, fallback: boolean): boolean {
  try {
    const v = sessionStorage.getItem(key);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch {
    /* ignore */
  }
  return fallback;
}
function writeBool(key: string, value: boolean) {
  try {
    sessionStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* ignore */
  }
}

const RAIL_KEY = "aakaar.chat.rail";
const DOCK_KEY = "aakaar.chat.dock";

function SessionWorkspace({
  sessionId,
  sessions,
  activeId,
  onPick,
  onNew,
  isCreating,
}: WorkspaceProps) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const labels = useLabels();
  const cs = useChatStrings();
  const { token } = useAuth();

  const [input, setInput] = useState("");
  const [name, setName] = useState("");
  const [confirmingSave, setConfirmingSave] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ChatSessionSummary | null>(null);
  const [railOpen, setRailOpen] = useState(() =>
    readBool(RAIL_KEY, window.innerWidth >= 1024),
  );
  const [dockOpen, setDockOpen] = useState(() =>
    readBool(DOCK_KEY, window.innerWidth >= 1280),
  );
  const [dockTab, setDockTab] = useState<DockTab>("plan");
  const [liveMsg, setLiveMsg] = useState("");

  // Run-from-chat state.
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [runInputs, setRunInputs] = useState("{}");
  const [runMode, setRunMode] = useState<"live" | "dry_run">("live");
  const [launchTarget, setLaunchTarget] = useState<{
    workflowId: string;
    version: number;
  } | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [approvalNotice, setApprovalNotice] = useState<ApprovalRequest | null>(null);
  const postSaveActionRef = useRef<"run" | null>(null);

  const composerRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const sendAbortRef = useRef<AbortController | null>(null);
  const lastSentRef = useRef<string>("");

  useEffect(() => writeBool(RAIL_KEY, railOpen), [railOpen]);
  useEffect(() => writeBool(DOCK_KEY, dockOpen), [dockOpen]);

  const capsQ = useQuery({
    queryKey: ["capabilities"],
    queryFn: capsApi.list,
    staleTime: 5 * 60 * 1000,
  });

  const sessionQ = useQuery({
    queryKey: ["chat-session", sessionId],
    queryFn: () => sessionsApi.get(sessionId),
  });
  const session = sessionQ.data;

  const send = useMutation({
    mutationFn: (message: string) => {
      const controller = new AbortController();
      sendAbortRef.current = controller;
      return sessionsApi.send(sessionId, { message }, controller.signal);
    },
    onSuccess: (s) => {
      queryClient.setQueryData(["chat-session", sessionId], s);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      setLiveMsg(
        s.draft_dag
          ? fmt(cs.annPlanReady, { n: s.draft_dag.nodes.length })
          : cs.annReplied,
      );
    },
    onError: (err) => {
      const text = lastSentRef.current;
      if (text) setInput(text); // never lose the operator's words
      setLiveMsg(isAbortError(err) ? cs.annStopped : cs.annCouldntBuild);
    },
  });

  const save = useMutation({
    mutationFn: (input: { name?: string; confirm?: boolean }) =>
      sessionsApi.save(sessionId, input),
    onSuccess: (workflow) => {
      setConfirmingSave(false);
      queryClient.invalidateQueries({ queryKey: ["chat-session", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      if (postSaveActionRef.current === "run") {
        postSaveActionRef.current = null;
        setLaunchTarget({ workflowId: workflow.id, version: workflow.latest_version });
        setRunDialogOpen(true);
      }
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => sessionsApi.remove(id),
    onSuccess: (_, deletedId) => {
      const remaining = sessions.filter((s) => s.id !== deletedId);
      queryClient.setQueryData(["chat-sessions"], remaining);
      queryClient.removeQueries({ queryKey: ["chat-session", deletedId] });
      setDeleteTarget(null);
      if (deletedId === sessionId) {
        const next = remaining[0];
        navigate(next ? `/chat/${next.id}` : "/chat", { replace: true });
      }
    },
  });

  const startRun = useMutation({
    mutationFn: (args: {
      workflowId: string;
      version: number;
      inputs: Record<string, unknown>;
      mode: "live" | "dry_run";
    }) =>
      runsApi.start(args.workflowId, args.inputs, null, args.version, args.mode),
    onSuccess: (result) => {
      setRunDialogOpen(false);
      if (isApprovalPending(result)) {
        setApprovalNotice(result.approval);
        setLiveMsg(cs.annNeedsApproval);
        return;
      }
      setApprovalNotice(null);
      setActiveRunId(result.id);
      setDockOpen(true);
      setDockTab("live");
      setLiveMsg(cs.annRunStarted);
    },
  });

  // ---------- live run polling + WS ----------
  const runQ = useQuery({
    queryKey: ["chat-run", activeRunId],
    queryFn: () => runsApi.get(activeRunId!),
    enabled: !!activeRunId,
    refetchInterval: (q) => {
      const s = q.state.data?.run.status;
      if (!s) return 1500;
      return isTerminalStatus(s) ? false : 1500;
    },
  });
  const runStatus = runQ.data?.run.status;
  const liveRunId = activeRunId && !isTerminalStatus(runStatus) ? activeRunId : null;
  const { events: liveEvents } = useRunEvents(liveRunId, token);
  const runEvents = useMemo(
    // useRunEvents types payload as `unknown`; the runtime shape matches the
    // api RunEvent (payload is an object), so we align the types at the seam.
    () => mergeEventsBySeq(runQ.data?.events ?? [], liveEvents as unknown as RunEvent[]),
    [runQ.data?.events, liveEvents],
  );

  // Reset per-session ephemeral state on switch.
  useEffect(() => {
    setInput("");
    setName("");
    setActiveRunId(null);
    setApprovalNotice(null);
    setDockTab("plan");
    send.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Scroll-anchored auto-follow: only when the operator is already near bottom.
  useEffect(() => {
    if (atBottomRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [session?.messages.length, send.isPending]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 96;
  };

  if (sessionQ.isLoading) {
    return (
      <div className="grid h-full place-items-center text-sm text-ink-500">
        {cs.loadingWorkspace}
      </div>
    );
  }
  if (sessionQ.error || !session) {
    return (
      <div className="p-7">
        <ErrorBanner error={sessionQ.error ?? "session not found"} />
      </div>
    );
  }

  const draftNodes = session.draft_dag?.nodes.length ?? 0;
  const hasRunnableDraft = session.draft_dag != null;
  const realSendError =
    send.isError && !isAbortError(send.error) ? send.error : null;

  const submit = () => {
    const text = input.trim();
    if (!text || send.isPending) return;
    lastSentRef.current = text;
    atBottomRef.current = true;
    setInput("");
    setLiveMsg(cs.draftingPlan);
    send.mutate(text);
  };

  const onStop = () => sendAbortRef.current?.abort();

  const openSaveFlow = () => {
    if (session.workflow_id == null && name.trim()) {
      save.mutate({ name: name.trim() });
      return;
    }
    setConfirmingSave(true);
  };

  const openRunFlow = () => {
    if (!hasRunnableDraft) return;
    if (session.workflow_id && session.saved_version != null && !session.is_dirty) {
      setLaunchTarget({
        workflowId: session.workflow_id,
        version: session.saved_version,
      });
      setRunDialogOpen(true);
      return;
    }
    postSaveActionRef.current = "run";
    if (session.workflow_id == null) {
      if (!name.trim()) {
        setConfirmingSave(true);
        return;
      }
      save.mutate({ name: name.trim() });
      return;
    }
    save.mutate({ confirm: true });
  };

  const openDockPlan = () => {
    setDockOpen(true);
    setDockTab("plan");
  };

  const onCommand = (cmd: string) => {
    if (cmd === "run") openRunFlow();
    else if (cmd === "save") openSaveFlow();
    else if (cmd === "plan") openDockPlan();
  };

  const handleLaunch = () => {
    if (!launchTarget) return;
    let inputs: Record<string, unknown> = {};
    try {
      inputs = JSON.parse(runInputs || "{}");
    } catch {
      setLiveMsg("Execution inputs must be valid JSON.");
      return;
    }
    startRun.mutate({
      workflowId: launchTarget.workflowId,
      version: launchTarget.version,
      inputs,
      mode: runMode,
    });
  };

  const targetAgents = Array.from(
    new Set(
      (session.draft_dag?.nodes ?? [])
        .map((n) => nodeAgentTarget(n.target))
        .filter((a): a is string => a != null),
    ),
  );

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      {/* Zone 1 — session rail (collapsible; overlay < lg) */}
      <SessionRail
        open={railOpen}
        sessions={sessions}
        activeId={activeId}
        activeRunId={activeRunId}
        runStatus={runStatus}
        onPick={(id) => {
          onPick(id);
          if (window.innerWidth < 1024) setRailOpen(false);
        }}
        onNew={onNew}
        isCreating={isCreating}
        onRequestDelete={(s) => {
          remove.reset();
          setDeleteTarget(s);
        }}
        onClose={() => setRailOpen(false)}
        removingId={remove.isPending ? (remove.variables as string) : null}
      />

      {/* Zone 2 — conversation (never collapses) */}
      <main className="flex min-w-0 flex-1 flex-col bg-gradient-to-b from-ink-950/5 to-ink-950/30">
        {/* Sub-header: zone toggles + title + run */}
        <div className="flex min-h-[52px] items-center justify-between gap-3 border-b border-ink-800/70 bg-ink-950/25 px-3 sm:px-4">
          <div className="flex min-w-0 items-center gap-2">
            <IconToggle
              on={railOpen}
              onClick={() => setRailOpen((v) => !v)}
              onIcon={<PanelLeftClose size={16} />}
              offIcon={<PanelLeftOpen size={16} />}
              label={railOpen ? cs.ariaHideConversations : cs.ariaShowConversations}
            />
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-ink-100">
                {session.title}
              </p>
              <p className="mt-0.5 text-[11px] text-ink-500">
                {draftNodes > 0 ? fmt(cs.subSteps, { n: draftNodes }) : cs.subNoPlan}
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              className="btn-primary !min-h-8 !px-3 text-xs"
              onClick={openRunFlow}
              disabled={!hasRunnableDraft || save.isPending || startRun.isPending}
              title={
                !hasRunnableDraft
                  ? cs.runHintNoPlan
                  : session.is_dirty || session.workflow_id == null
                    ? cs.runHintSaveRun
                    : cs.runHintRun
              }
            >
              <Play size={13} /> {labels.runYajna}
            </button>
            <IconToggle
              on={dockOpen}
              onClick={() => setDockOpen((v) => !v)}
              onIcon={<PanelRightClose size={16} />}
              offIcon={<PanelRightOpen size={16} />}
              label={dockOpen ? cs.ariaHidePlan : cs.ariaShowPlan}
            />
          </div>
        </div>

        {/* Transcript */}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="min-h-0 flex-1 overflow-y-auto"
        >
          <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col px-4 py-6 sm:px-6">
            {session.messages.length === 0 && !send.isPending ? (
              <PromptStarters
                onSelect={(prompt) => {
                  setInput(prompt);
                  composerRef.current?.focus();
                }}
              />
            ) : (
              <div
                className="space-y-5"
                role="log"
                aria-live="polite"
                aria-relevant="additions"
                aria-label={cs.transcriptAria}
              >
                {session.messages.map((m, idx) =>
                  m.role === "user" ? (
                    <UserBubble key={m.id} text={m.text} at={m.at} />
                  ) : (
                    <PlannerBubble
                      key={m.id}
                      response={m.payload as RawChatResponse}
                      at={m.at}
                      isLatest={idx === session.messages.length - 1 && !send.isPending}
                      onQuickReply={(t) => {
                        lastSentRef.current = t;
                        atBottomRef.current = true;
                        send.mutate(t);
                      }}
                      onReviewPlan={openDockPlan}
                      onRunDraft={openRunFlow}
                      onGoToGrants={() => navigate("/admin/grants")}
                    />
                  ),
                )}

                {/* Optimistic echo of the message being sent */}
                {send.isPending && lastSentRef.current ? (
                  <UserBubble text={lastSentRef.current} pending />
                ) : null}

                {send.isPending ? <ThinkingBubble /> : null}
                {realSendError ? <PlannerErrorBubble error={realSendError} /> : null}
                {approvalNotice ? (
                  <ApprovalNoticeBubble
                    approval={approvalNotice}
                    onView={() => navigate("/approvals")}
                  />
                ) : null}
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Activity strip + composer */}
        <div className="border-t border-ink-800/70 bg-ink-950/50 px-3 py-3 backdrop-blur-xl sm:px-4">
          <div className="mx-auto w-full max-w-3xl">
            <ActivityStrip
              sending={send.isPending}
              dag={session.draft_dag}
              runStatus={runStatus}
              events={runEvents}
              onOpenLive={() => {
                setDockOpen(true);
                setDockTab("live");
              }}
            />
            <p className="sr-only" role="status" aria-live="polite">
              {liveMsg}
            </p>
            <ChatComposer
              value={input}
              onChange={setInput}
              onSubmit={submit}
              onStop={onStop}
              isPending={send.isPending}
              capabilities={capsQ.data ?? []}
              onCommand={onCommand}
              textareaRef={composerRef}
            />
            <p className="mt-2 px-1 text-center text-[11px] text-ink-600">
              {cs.composerFootnote}
            </p>
          </div>
        </div>
      </main>

      {/* Zone 3 — activity dock (collapsible; overlay < xl) */}
      <ActivityDock
        open={dockOpen}
        tab={dockTab}
        onTab={setDockTab}
        hasLive={activeRunId != null}
        onClose={() => setDockOpen(false)}
        session={session}
        name={name}
        setName={setName}
        onSave={openSaveFlow}
        isSaving={save.isPending}
        saveError={save.error}
        onDelete={() =>
          setDeleteTarget({
            id: session.id,
            title: session.title,
            workflow_id: session.workflow_id,
            saved_version: session.saved_version,
            is_dirty: session.is_dirty,
            created_at: session.created_at,
            updated_at: session.updated_at,
          })
        }
        runStatus={runStatus}
        runEvents={runEvents}
      />

      {/* Modals */}
      <SaveDialog
        open={confirmingSave}
        session={session}
        name={name}
        setName={setName}
        isSaving={save.isPending}
        onCancel={() => {
          postSaveActionRef.current = null;
          setConfirmingSave(false);
        }}
        onConfirm={() => {
          if (session.workflow_id == null) save.mutate({ name: name.trim() });
          else save.mutate({ confirm: true });
        }}
      />

      <DeleteDialog
        target={deleteTarget}
        isDeleting={remove.isPending}
        error={remove.error}
        onCancel={() => {
          if (!remove.isPending) setDeleteTarget(null);
        }}
        onConfirm={() => deleteTarget && remove.mutate(deleteTarget.id)}
      />

      <RunDialog
        open={runDialogOpen && launchTarget != null}
        version={launchTarget?.version ?? 0}
        targetAgents={targetAgents}
        inputs={runInputs}
        mode={runMode}
        isStarting={startRun.isPending}
        error={startRun.error}
        onInputsChange={setRunInputs}
        onModeChange={setRunMode}
        onCancel={() => setRunDialogOpen(false)}
        onConfirm={handleLaunch}
      />
    </div>
  );
}

// ===========================================================================
// Zone 1 — session rail
// ===========================================================================

function SessionRail({
  open,
  sessions,
  activeId,
  activeRunId,
  runStatus,
  onPick,
  onNew,
  isCreating,
  onRequestDelete,
  onClose,
  removingId,
}: {
  open: boolean;
  sessions: ChatSessionSummary[];
  activeId: string | null;
  activeRunId: string | null;
  runStatus?: RunStatus;
  onPick: (id: string) => void;
  onNew: () => void;
  isCreating: boolean;
  onRequestDelete: (s: ChatSessionSummary) => void;
  onClose: () => void;
  removingId: string | null;
}) {
  const labels = useLabels();
  const cs = useChatStrings();
  const liveActive = activeRunId != null && !isTerminalStatus(runStatus);
  return (
    <>
      {open ? (
        <div
          className="fixed inset-0 z-20 bg-ink-950/50 lg:hidden"
          onClick={onClose}
          aria-hidden
        />
      ) : null}
      <aside
        data-tour="chat-sessions"
        className={[
          "z-30 flex shrink-0 flex-col overflow-hidden border-r border-ink-800/80 bg-ink-950/60 backdrop-blur-xl transition-[width] duration-200",
          "max-lg:fixed max-lg:inset-y-0 max-lg:left-0",
          open ? "w-64 max-lg:w-[min(18rem,85vw)] max-lg:shadow-2xl" : "w-0 border-r-0",
        ].join(" ")}
      >
        <div className="flex min-w-64 items-center justify-between px-4 pb-2 pt-3">
          <div>
            <p className="text-sm font-semibold text-ink-100">{labels.samvadas}</p>
            <p className="mt-0.5 text-[11px] text-ink-500">{cs.onePerAutomation}</p>
          </div>
          <button
            type="button"
            className="btn-ghost !min-h-8 !px-2.5 text-xs"
            onClick={onNew}
            disabled={isCreating}
            title={`${cs.newLabel} ${labels.samvada.toLowerCase()}`}
          >
            <Plus size={13} /> {cs.newLabel}
          </button>
        </div>
        <ul className="min-h-0 min-w-64 flex-1 space-y-1 overflow-y-auto px-2 pb-3 pt-1">
          {sessions.map((s) => {
            const isActive = s.id === activeId;
            return (
              <li key={s.id} className="group relative">
                <button
                  type="button"
                  onClick={() => onPick(s.id)}
                  className={[
                    "block w-full rounded-xl border py-2.5 pl-3 pr-10 text-left transition",
                    isActive
                      ? "border-accent-300/30 bg-accent-300/10 text-ink-50 shadow-sm"
                      : "border-transparent text-ink-300 hover:border-ink-700/70 hover:bg-ink-900/50",
                  ].join(" ")}
                >
                  <span className="block truncate text-xs font-medium">{s.title}</span>
                  <span className="mt-1 flex items-center gap-1.5 text-[10px] text-ink-500">
                    {isActive && liveActive ? (
                      <>
                        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-signal-cyan" />
                        {cs.statusRunning}
                      </>
                    ) : s.is_dirty ? (
                      <>
                        <span className="h-1.5 w-1.5 rounded-full bg-amber-300" />
                        {cs.statusUnsaved}
                      </>
                    ) : s.workflow_id ? (
                      <>
                        <CheckCircle2 size={10} className="text-emerald-400" />
                        {cs.statusSaved}
                      </>
                    ) : (
                      <>
                        <MessageSquare size={10} />
                        {cs.statusPlanning}
                      </>
                    )}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => onRequestDelete(s)}
                  className={[
                    "absolute right-2 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-lg text-ink-500 transition",
                    "opacity-0 hover:bg-rose-500/10 hover:text-rose-300 focus-visible:opacity-100 group-hover:opacity-100",
                    removingId === s.id ? "opacity-100" : "",
                  ].join(" ")}
                  disabled={removingId != null}
                  aria-label={`${cs.ariaDelete}: ${s.title}`}
                  title={cs.ariaDelete}
                >
                  <Trash2 size={13} />
                </button>
              </li>
            );
          })}
          {sessions.length === 0 ? (
            <li className="px-3 py-4 text-xs text-ink-500">{cs.noChatsYet}</li>
          ) : null}
        </ul>
      </aside>
    </>
  );
}

function IconToggle({
  on,
  onClick,
  onIcon,
  offIcon,
  label,
}: {
  on: boolean;
  onClick: () => void;
  onIcon: ReactNode;
  offIcon: ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      className="btn-ghost !h-9 !min-h-9 !w-9 !p-0"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={on}
    >
      {on ? onIcon : offIcon}
    </button>
  );
}

// ===========================================================================
// Activity strip (above composer)
// ===========================================================================

function ActivityStrip({
  sending,
  dag,
  runStatus,
  events,
  onOpenLive,
}: {
  sending: boolean;
  dag: Dag | null;
  runStatus?: RunStatus;
  events: RunEvent[];
  onOpenLive: () => void;
}) {
  const cs = useChatStrings();
  const runStatusLabel = useRunStatusLabel();
  const running = runStatus != null && !isTerminalStatus(runStatus);

  if (sending) {
    return (
      <div
        className="mb-2 flex items-center gap-2 rounded-xl border border-sky-400/20 bg-sky-400/10 px-3 py-2 text-xs text-sky-200"
        role="status"
      >
        <Clock size={13} className="shrink-0 animate-pulse" />
        <span>{cs.draftingPlan}</span>
      </div>
    );
  }

  if (dag && runStatus) {
    const statuses = deriveNodeStatuses(dag, events, runStatus);
    const done = Object.values(statuses).filter((s) => s === "succeeded").length;
    const total = dag.nodes.length;
    const currentId = dag.nodes.find((n) => statuses[n.id] === "running")?.id;
    const currentNode = dag.nodes.find((n) => n.id === currentId);
    const tone = running
      ? "border-signal-cyan/25 bg-signal-cyan/10 text-signal-cyan"
      : runStatus === "failed"
        ? "border-rose-400/25 bg-rose-400/10 text-rose-200"
        : "border-emerald-400/25 bg-emerald-400/10 text-emerald-200";
    return (
      <button
        type="button"
        onClick={onOpenLive}
        className={["mb-2 flex w-full items-center gap-2 rounded-xl border px-3 py-2 text-left text-xs", tone].join(" ")}
      >
        <Activity size={13} className={running ? "shrink-0 animate-pulse" : "shrink-0"} />
        <span className="min-w-0 flex-1 truncate">
          {running
            ? currentNode
              ? fmt(cs.runningWith, {
                  x: friendlyCapabilityName(currentNode.ref),
                  n: done,
                  total,
                })
              : fmt(cs.runningCount, { n: done, total })
            : fmt(cs.runState, {
                status: runStatus ? runStatusLabel(runStatus) : "",
                n: done,
                total,
              })}
        </span>
        <span className="shrink-0 opacity-70">{cs.openLive}</span>
      </button>
    );
  }

  return null;
}

// ===========================================================================
// Zone 3 — activity dock (Plan | Live)
// ===========================================================================

function ActivityDock({
  open,
  tab,
  onTab,
  hasLive,
  onClose,
  session,
  name,
  setName,
  onSave,
  isSaving,
  saveError,
  onDelete,
  runStatus,
  runEvents,
}: {
  open: boolean;
  tab: DockTab;
  onTab: (t: DockTab) => void;
  hasLive: boolean;
  onClose: () => void;
  session: ChatSession;
  name: string;
  setName: (v: string) => void;
  onSave: () => void;
  isSaving: boolean;
  saveError: unknown;
  onDelete: () => void;
  runStatus?: RunStatus;
  runEvents: RunEvent[];
}) {
  const labels = useLabels();
  const runStatusLabel = useRunStatusLabel();
  const activeTab: DockTab = tab === "live" && !hasLive ? "plan" : tab;

  return (
    <>
      {open ? (
        <div
          className="fixed inset-0 z-20 bg-ink-950/50 xl:hidden"
          onClick={onClose}
          aria-hidden
        />
      ) : null}
      <aside
        data-tour="chat-visualizer"
        className={[
          "z-30 flex shrink-0 flex-col overflow-hidden border-l border-ink-800/80 bg-ink-950/50 backdrop-blur-xl transition-[width] duration-200",
          "max-xl:fixed max-xl:inset-y-0 max-xl:right-0",
          open
            ? "w-[26rem] max-xl:w-[min(30rem,92vw)] max-xl:shadow-2xl"
            : "w-0 border-l-0",
        ].join(" ")}
      >
        <div className="flex min-w-[26rem] items-center justify-between gap-2 border-b border-ink-800/70 px-3 py-2">
          {hasLive ? (
            <div className="flex items-center gap-1 rounded-xl border border-ink-800/80 bg-ink-950/40 p-1">
              <DockTabButton active={activeTab === "plan"} onClick={() => onTab("plan")} icon={<Eye size={13} />}>
                {labels.yantra}
              </DockTabButton>
              <DockTabButton active={activeTab === "live"} onClick={() => onTab("live")} icon={<Activity size={13} />}>
                {labels.pratyaksha}
                {runStatus && !isTerminalStatus(runStatus) ? (
                  <span className="ml-1 h-1.5 w-1.5 animate-pulse rounded-full bg-signal-cyan" />
                ) : null}
              </DockTabButton>
            </div>
          ) : (
            <span className="panel-title">{labels.yantra}</span>
          )}
        </div>

        {activeTab === "plan" ? (
          <div className="flex min-w-[26rem] min-h-0 flex-1 flex-col">
            <DraftHeader
              session={session}
              name={name}
              setName={setName}
              onSave={onSave}
              isSaving={isSaving}
              onDelete={onDelete}
            />
            {saveError ? (
              <div className="border-b border-ink-800 p-3">
                <ErrorBanner error={saveError} />
              </div>
            ) : null}
            <div className="relative min-h-0 flex-1 overflow-hidden">
              {session.draft_dag ? (
                <DagViewer dag={session.draft_dag} />
              ) : (
                <EmptyPlan />
              )}
            </div>
          </div>
        ) : (
          <div className="flex min-w-[26rem] min-h-0 flex-1 flex-col">
            <div className="flex items-center gap-2 border-b border-ink-800/70 px-4 py-2.5 text-[11px] text-ink-400">
              <span className="panel-title">{labels.pratyaksha}</span>
              {runStatus ? (
                <span className="font-mono">· {runStatusLabel(runStatus)}</span>
              ) : null}
            </div>
            <div className="relative min-h-0 flex-1">
              {session.draft_dag ? (
                <LiveDagViewer
                  dag={session.draft_dag}
                  events={runEvents}
                  runStatus={runStatus ?? "queued"}
                />
              ) : null}
              <LiveScreenPanel events={runEvents} variant="thumb" />
            </div>
          </div>
        )}
      </aside>
    </>
  );
}

function DockTabButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "flex min-h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-medium transition",
        active
          ? "bg-ink-800/90 text-ink-50 shadow-sm"
          : "text-ink-400 hover:bg-ink-900/70 hover:text-ink-200",
      ].join(" ")}
    >
      {icon}
      {children}
    </button>
  );
}

function EmptyPlan() {
  const cs = useChatStrings();
  return (
    <div className="grid h-full place-items-center px-6 text-center">
      <div className="max-w-xs">
        <span className="mx-auto grid h-12 w-12 place-items-center rounded-2xl border border-ink-800 bg-ink-900/60 text-ink-400">
          <Sparkles size={20} />
        </span>
        <h2 className="mt-4 text-sm font-semibold text-ink-100">
          {cs.planAppearHeading}
        </h2>
        <p className="mt-2 text-xs leading-5 text-ink-500">{cs.planAppearBody}</p>
      </div>
    </div>
  );
}

// ===========================================================================
// Draft header (plan sub-header)
// ===========================================================================

function DraftHeader({
  session,
  name,
  setName,
  onSave,
  isSaving,
  onDelete,
}: {
  session: ChatSession;
  name: string;
  setName: (v: string) => void;
  onSave: () => void;
  isSaving: boolean;
  onDelete: () => void;
}) {
  const cs = useChatStrings();
  const isFirstSave = session.workflow_id == null;
  const canSave = session.draft_dag != null && (isFirstSave || session.is_dirty);
  const buttonLabel = isFirstSave
    ? cs.saveWorkflow
    : session.is_dirty
      ? fmt(cs.saveVersion, { v: (session.saved_version ?? 0) + 1 })
      : fmt(cs.savedVersion, { v: session.saved_version ?? 0 });

  return (
    <div className="flex items-center justify-between gap-3 border-b border-ink-800/80 bg-ink-950/30 px-4 py-2.5">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {isFirstSave ? (
          <input
            className="input max-w-[12rem] !py-1.5 !text-xs"
            placeholder={cs.workflowNamePlaceholder}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        ) : (
          <div className="flex items-center gap-2 font-mono text-[10px] text-ink-400">
            <Clock size={12} className="text-accent-300" />
            <span>v{session.saved_version}</span>
            {session.is_dirty ? (
              <span className="badge ring-amber-400/40 text-amber-300">{cs.drift}</span>
            ) : null}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          className="btn-ghost !min-h-9 !px-2.5 text-rose-300 hover:bg-rose-500/10"
          onClick={onDelete}
          title={cs.ariaDelete}
        >
          <Trash2 size={13} />
        </button>
        <button
          type="button"
          className="btn-primary !min-h-9 !px-3 text-xs"
          onClick={onSave}
          disabled={!canSave || isSaving}
        >
          <Save size={13} /> {buttonLabel}
        </button>
      </div>
    </div>
  );
}

// ===========================================================================
// Bubbles
// ===========================================================================

function BubbleTime({ at }: { at?: string }) {
  if (!at) return null;
  return (
    <span className="ml-2 shrink-0 font-mono text-[10px] text-ink-600">
      {formatISTTime(at)}
    </span>
  );
}

function UserBubble({
  text,
  at,
  pending,
}: {
  text: string;
  at?: string;
  pending?: boolean;
}) {
  return (
    <div className="flex flex-col items-end">
      <div
        className={[
          "max-w-[85%] whitespace-pre-wrap break-words [overflow-wrap:anywhere] rounded-2xl rounded-br-md bg-accent-500/12 px-4 py-2.5 text-sm leading-6 text-ink-100 ring-1 ring-inset ring-accent-500/25 sm:max-w-[75%]",
          pending ? "opacity-70" : "",
        ].join(" ")}
      >
        {text}
      </div>
      {at && !pending ? (
        <span className="mt-1 pr-1 font-mono text-[10px] text-ink-600">
          {formatISTTime(at)}
        </span>
      ) : null}
    </div>
  );
}

function ThinkingBubble() {
  const cs = useChatStrings();
  return (
    <div className="flex items-center gap-3 text-sm text-ink-300">
      <span className="flex gap-1" aria-hidden>
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent-300 [animation-delay:-0.2s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent-300 [animation-delay:-0.1s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent-300" />
      </span>
      {cs.buildingPlan}
    </div>
  );
}

function PlannerBubble({
  response,
  at,
  isLatest,
  onQuickReply,
  onReviewPlan,
  onRunDraft,
  onGoToGrants,
}: {
  response: RawChatResponse;
  at?: string;
  isLatest?: boolean;
  onQuickReply?: (text: string) => void;
  onReviewPlan?: () => void;
  onRunDraft?: () => void;
  onGoToGrants?: () => void;
}) {
  if (response.kind === "dag" && response.dag) {
    return (
      <DagBubble
        dag={response.dag}
        rationale={response.rationale}
        at={at}
        onReviewPlan={onReviewPlan}
        onRunDraft={onRunDraft}
      />
    );
  }
  if (response.kind === "clarify") {
    return (
      <ClarifyBubble
        questions={response.questions}
        at={at}
        isLatest={isLatest}
        onSend={onQuickReply}
      />
    );
  }
  return (
    <MissingBubble
      needed={response.needed}
      explanation={response.explanation}
      at={at}
      onGoToGrants={onGoToGrants}
    />
  );
}

function PlannerShell({
  tone = "default",
  children,
}: {
  tone?: "default" | "warn";
  children: ReactNode;
}) {
  return (
    <div className="flex justify-start">
      <div
        className={[
          "max-w-[92%] rounded-2xl rounded-bl-md border px-4 py-3.5 shadow-sm sm:max-w-[85%] [overflow-wrap:anywhere]",
          tone === "warn"
            ? "border-amber-500/20 bg-amber-500/5"
            : "border-ink-800/80 bg-ink-900/55",
        ].join(" ")}
      >
        {children}
      </div>
    </div>
  );
}

function DagBubble({
  dag,
  rationale,
  at,
  onReviewPlan,
  onRunDraft,
}: {
  dag: Dag;
  rationale: string;
  at?: string;
  onReviewPlan?: () => void;
  onRunDraft?: () => void;
}) {
  const cs = useChatStrings();
  const [expanded, setExpanded] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  const [copied, setCopied] = useState(false);

  const sideEffecting = dag.nodes.filter((n) => isSideEffectingRef(n.ref));
  const visible = expanded ? dag.nodes : dag.nodes.slice(0, 5);

  const copyPlan = async () => {
    const text = dag.nodes
      .map((n, i) => `${i + 1}. ${friendlyCapabilityName(n.ref)} (${n.ref})`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — ignore */
    }
  };

  return (
    <PlannerShell>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-ink-100">
          <span className="grid h-7 w-7 place-items-center rounded-full bg-emerald-400/10 text-emerald-300">
            <CheckCircle2 size={14} />
          </span>
          {fmt(cs.drafted, { n: dag.nodes.length })}
        </div>
        <BubbleTime at={at} />
      </div>

      {rationale ? (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowReasoning((v) => !v)}
            className="flex items-center gap-1 text-[11px] font-medium text-ink-400 hover:text-ink-200"
            aria-expanded={showReasoning}
          >
            {showReasoning ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            {cs.plannerReasoning}
          </button>
          {showReasoning ? (
            <p className="mt-1.5 whitespace-pre-wrap text-sm leading-6 text-ink-300">
              {rationale}
            </p>
          ) : null}
        </div>
      ) : null}

      <ol className="mt-3 space-y-1.5 border-l border-ink-700/70 pl-3">
        {visible.map((node, index) => (
          <li key={node.id} className="flex items-start gap-2 text-xs text-ink-300">
            <span className="w-4 shrink-0 pt-0.5 text-ink-600">{index + 1}</span>
            <span className="min-w-0 flex-1">
              <span className="text-ink-200">{friendlyCapabilityName(node.ref)}</span>
              <span className="ml-1.5 inline-flex flex-wrap items-center gap-1 align-middle">
                <span
                  className={[
                    "rounded border px-1 py-0.5 font-mono text-[9px]",
                    KIND_CHIP[node.kind] ?? KIND_CHIP.capability,
                  ].join(" ")}
                >
                  {node.kind}
                </span>
                {nodeAgentTarget(node.target) ? (
                  <span className="inline-flex items-center gap-0.5 rounded border border-signal-cyan/25 bg-signal-cyan/5 px-1 py-0.5 font-mono text-[9px] text-signal-cyan">
                    <MonitorDot size={9} />
                    {nodeAgentTarget(node.target)}
                  </span>
                ) : null}
                {isSideEffectingRef(node.ref) ? (
                  <span className="inline-flex items-center gap-0.5 rounded border border-rose-400/30 bg-rose-400/10 px-1 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wide text-rose-300">
                    <FlaskConical size={9} /> {cs.liveAction}
                  </span>
                ) : null}
              </span>
            </span>
          </li>
        ))}
      </ol>
      {dag.nodes.length > 5 ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1.5 pl-6 text-[11px] font-medium text-accent-300 hover:text-accent-200"
        >
          {expanded ? cs.showFewer : fmt(cs.moreSteps, { n: dag.nodes.length - 5 })}
        </button>
      ) : null}

      {sideEffecting.length > 0 ? (
        <div className="mt-3 flex items-start gap-2 rounded-lg border border-amber-500/25 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-5 text-amber-200">
          <ShieldAlert size={13} className="mt-0.5 shrink-0" />
          <span>
            {fmt(cs.makerChecker, {
              x: friendlyCapabilityName(sideEffecting[0].ref),
            })}
          </span>
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {onReviewPlan ? (
          <button
            type="button"
            className="btn-primary !min-h-8 !px-3 text-xs"
            onClick={onReviewPlan}
          >
            <Eye size={13} /> {cs.reviewPlan}
          </button>
        ) : null}
        {onRunDraft ? (
          <button
            type="button"
            className="btn-ghost !min-h-8 !px-3 text-xs"
            onClick={onRunDraft}
          >
            <Play size={13} /> {cs.runDraft}
          </button>
        ) : null}
        <button
          type="button"
          className="btn-ghost !min-h-8 !px-2.5 text-xs text-ink-400"
          onClick={copyPlan}
          title={cs.copy}
        >
          <Copy size={13} /> {copied ? cs.copied : cs.copy}
        </button>
      </div>
    </PlannerShell>
  );
}

function ClarifyBubble({
  questions,
  at,
  isLatest,
  onSend,
}: {
  questions: string[];
  at?: string;
  isLatest?: boolean;
  onSend?: (text: string) => void;
}) {
  const cs = useChatStrings();
  const [answers, setAnswers] = useState<string[]>(() => questions.map(() => ""));
  const [sent, setSent] = useState(false);

  const setAnswer = (i: number, val: string) =>
    setAnswers((prev) => prev.map((a, idx) => (idx === i ? val : a)));
  const allAnswered = answers.every((a) => a.trim().length > 0);

  const handleSend = () => {
    if (!onSend || !allAnswered) return;
    const composed = answers
      .map((a, i) => `${i + 1}. ${questions[i]}\n   → ${a.trim()}`)
      .join("\n");
    onSend(composed);
    setSent(true);
  };

  const editable = isLatest && !sent;

  return (
    <PlannerShell tone="warn">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-amber-200">
          <HelpCircle size={14} /> {cs.clarifyHeading}
        </div>
        <BubbleTime at={at} />
      </div>
      <div className="mt-3 space-y-3">
        {questions.map((q, i) => (
          <div key={i} className="space-y-1">
            <p className="text-sm leading-6 text-ink-200">{q}</p>
            {editable ? (
              <input
                type="text"
                className="input !py-2 !text-sm"
                placeholder={cs.typeAnswer}
                value={answers[i]}
                onChange={(e) => setAnswer(i, e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && allAnswered) {
                    e.preventDefault();
                    handleSend();
                  }
                }}
              />
            ) : answers[i] ? (
              <p className="border-l border-signal-cyan/30 pl-2 font-mono text-[11px] text-signal-cyan">
                {answers[i]}
              </p>
            ) : null}
          </div>
        ))}
      </div>
      {editable ? (
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            className="btn-primary !min-h-8 !px-3 text-xs"
            disabled={!allAnswered}
            onClick={handleSend}
          >
            <Send size={12} /> {cs.sendAnswers}
          </button>
          {!allAnswered ? (
            <span className="font-mono text-[10px] text-ink-500">
              {fmt(cs.answerAll, { n: questions.length })}
            </span>
          ) : null}
        </div>
      ) : null}
      {sent ? (
        <p className="mt-2 flex items-center gap-1 font-mono text-[10px] text-emerald-400">
          <CheckCircle2 size={11} /> {cs.answersSent}
        </p>
      ) : null}
    </PlannerShell>
  );
}

function MissingBubble({
  needed,
  explanation,
  at,
  onGoToGrants,
}: {
  needed: string[];
  explanation: string;
  at?: string;
  onGoToGrants?: () => void;
}) {
  const cs = useChatStrings();
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (ref: string) => {
    try {
      await navigator.clipboard.writeText(ref);
      setCopied(ref);
      setTimeout(() => setCopied((c) => (c === ref ? null : c)), 1200);
    } catch {
      /* ignore */
    }
  };
  return (
    <PlannerShell tone="warn">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-amber-200">
          <ShieldAlert size={14} /> {cs.missingHeading}
        </div>
        <BubbleTime at={at} />
      </div>
      {explanation ? (
        <p className="mt-2 text-sm leading-6 text-ink-300">{explanation}</p>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {needed.map((ref) => (
          <button
            key={ref}
            type="button"
            onClick={() => copy(ref)}
            title={cs.clickToCopy}
            className="inline-flex items-center gap-1 rounded-lg border border-ink-700 bg-ink-900/60 px-2 py-1 font-mono text-[10px] text-ink-300 transition hover:border-accent-300/40 hover:text-ink-100"
          >
            {copied === ref ? <CheckCircle2 size={10} className="text-emerald-400" /> : <Copy size={10} />}
            {ref}
          </button>
        ))}
      </div>
      {onGoToGrants ? (
        <button
          type="button"
          onClick={onGoToGrants}
          className="btn-ghost !min-h-8 !px-3 mt-3 text-xs text-amber-200 hover:bg-amber-500/10"
        >
          <ExternalLink size={12} /> {cs.openAccess}
        </button>
      ) : null}
    </PlannerShell>
  );
}

function PlannerErrorBubble({ error }: { error: unknown }) {
  const cs = useChatStrings();
  return (
    <div className="flex justify-start">
      <div className="max-w-[92%] rounded-2xl rounded-bl-md border border-rose-500/20 bg-rose-500/5 px-4 py-3 shadow-sm sm:max-w-[85%]">
        <div className="mb-1.5 flex items-center gap-2 text-sm font-medium text-rose-300">
          <AlertCircle size={14} /> {cs.errorHeading}
        </div>
        <p className="text-sm leading-6 text-ink-200">{humanizeError(error, cs)}</p>
        <p className="mt-2 text-xs text-ink-500">{cs.errorFootnote}</p>
      </div>
    </div>
  );
}

function ApprovalNoticeBubble({
  onView,
}: {
  approval: ApprovalRequest;
  onView: () => void;
}) {
  const cs = useChatStrings();
  return (
    <PlannerShell tone="warn">
      <div className="flex items-center gap-2 text-sm font-medium text-amber-200">
        <ShieldAlert size={14} /> {cs.approvalHeading}
      </div>
      <p className="mt-2 text-sm leading-6 text-ink-300">{cs.approvalBody}</p>
      <button
        type="button"
        onClick={onView}
        className="btn-ghost !min-h-8 !px-3 mt-3 text-xs text-amber-200 hover:bg-amber-500/10"
      >
        <ExternalLink size={12} /> {cs.viewApprovals}
      </button>
    </PlannerShell>
  );
}

// ===========================================================================
// Prompt starters
// ===========================================================================

function PromptStarters({ onSelect }: { onSelect: (p: string) => void }) {
  const cs = useChatStrings();
  return (
    <div className="my-auto py-6">
      <div className="max-w-xl">
        <span className="grid h-11 w-11 place-items-center rounded-2xl border border-accent-300/20 bg-accent-300/10 text-accent-200">
          <Sparkles size={19} />
        </span>
        <h2 className="mt-4 text-xl font-semibold tracking-tight text-ink-50">
          {cs.startersHeading}
        </h2>
        <p className="mt-2 max-w-lg text-sm leading-6 text-ink-400">
          {cs.startersIntro}
        </p>
      </div>
      <p className="mb-2.5 mt-7 flex items-center gap-1.5 text-xs font-medium text-ink-400">
        <Zap size={12} className="text-accent-300" /> {cs.tryExample}
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        {cs.starters.map((s) => (
          <button
            key={s.title}
            type="button"
            onClick={() => onSelect(s.prompt)}
            className="group rounded-xl border border-ink-800/90 bg-ink-900/45 p-3.5 text-left transition hover:border-accent-300/30 hover:bg-accent-300/[0.06]"
          >
            <span className="flex items-center justify-between gap-3 text-xs font-medium text-ink-200 group-hover:text-ink-50">
              {s.title}
              <ArrowRight
                size={13}
                className="shrink-0 text-ink-600 transition group-hover:translate-x-0.5 group-hover:text-accent-300"
              />
            </span>
            <span className="mt-1.5 block line-clamp-2 text-[11px] leading-4 text-ink-500">
              {s.prompt}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ===========================================================================
// Dialogs
// ===========================================================================

function SaveDialog({
  open,
  session,
  name,
  setName,
  isSaving,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  session: ChatSession;
  name: string;
  setName: (v: string) => void;
  isSaving: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cs = useChatStrings();
  const isFirstSave = session.workflow_id == null;
  const dag = session.draft_dag;
  const nodeCount = dag?.nodes.length ?? 0;
  const edgeCount = dag?.edges.length ?? 0;

  return (
    <Dialog
      open={open}
      onClose={onCancel}
      dismissable={!isSaving}
      icon={<Sparkles size={17} />}
      title={isFirstSave ? cs.saveTitleFirst : cs.saveTitleUpdate}
      description={
        isFirstSave
          ? cs.saveDescFirst
          : fmt(cs.saveDescUpdate, { v: (session.saved_version ?? 0) + 1 })
      }
      footer={
        <>
          <button
            type="button"
            className="btn-ghost !min-h-9 !px-4 text-xs"
            onClick={onCancel}
            disabled={isSaving}
          >
            {cs.cancel}
          </button>
          <button
            type="button"
            className="btn-primary !min-h-9 !px-4 text-xs"
            onClick={onConfirm}
            disabled={isSaving || (isFirstSave && !name.trim())}
          >
            <Save size={13} />
            {isSaving ? cs.saving : isFirstSave ? cs.save : cs.confirmUpdate}
          </button>
        </>
      }
    >
      <div className="rounded-md border border-ink-800 bg-ink-900/40 px-3 py-2 font-mono text-[10px] text-ink-400">
        <div className="font-semibold text-ink-300">
          {fmt(cs.planSummary, { n: nodeCount, e: edgeCount })}
        </div>
        {session.draft_rationale ? (
          <div className="mt-1.5 border-t border-ink-800/60 pt-1.5 italic leading-relaxed text-ink-400">
            “{session.draft_rationale}”
          </div>
        ) : null}
      </div>
      {isFirstSave ? (
        <label className="mt-4 block">
          <span className="mb-1.5 block text-xs font-medium text-ink-300">
            {cs.nameLabel}
          </span>
          <input
            data-autofocus
            className="input !text-sm"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={cs.namePlaceholder}
          />
        </label>
      ) : null}
    </Dialog>
  );
}

function DeleteDialog({
  target,
  isDeleting,
  error,
  onCancel,
  onConfirm,
}: {
  target: ChatSessionSummary | null;
  isDeleting: boolean;
  error: unknown;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cs = useChatStrings();
  const errorMessage =
    error instanceof ApiError
      ? error.detail
      : error instanceof Error
        ? error.message
        : error
          ? cs.deleteFailed
          : null;

  return (
    <Dialog
      open={target != null}
      onClose={onCancel}
      dismissable={!isDeleting}
      tone="danger"
      icon={<Trash2 size={17} />}
      title={cs.deleteTitle}
      description={
        target ? (
          <strong className="font-medium text-ink-100">“{target.title}”</strong>
        ) : null
      }
      footer={
        <>
          <button
            type="button"
            className="btn-ghost !min-h-9 !px-4 text-xs"
            onClick={onCancel}
            disabled={isDeleting}
          >
            {cs.cancel}
          </button>
          <button
            type="button"
            className="btn-danger !min-h-9 !px-4 text-xs"
            onClick={onConfirm}
            disabled={isDeleting}
            data-autofocus
          >
            <Trash2 size={13} />
            {isDeleting ? cs.deleting : cs.deleteConfirm}
          </button>
        </>
      }
    >
      <p className="text-xs leading-5 text-ink-500">
        {target?.workflow_id ? cs.deleteBodyKept : cs.deleteBodyLost}
      </p>
      {errorMessage ? (
        <div className="mt-3 rounded-xl border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {errorMessage}
        </div>
      ) : null}
    </Dialog>
  );
}

function RunDialog({
  open,
  version,
  targetAgents,
  inputs,
  mode,
  isStarting,
  error,
  onInputsChange,
  onModeChange,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  version: number;
  targetAgents: string[];
  inputs: string;
  mode: "live" | "dry_run";
  isStarting: boolean;
  error: unknown;
  onInputsChange: (v: string) => void;
  onModeChange: (m: "live" | "dry_run") => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cs = useChatStrings();
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      dismissable={!isStarting}
      maxWidthClassName="max-w-lg"
      icon={<Play size={17} />}
      title={cs.runTitle}
      description={fmt(cs.runDesc, { v: version })}
      footer={
        <>
          <button
            type="button"
            className="btn-ghost !min-h-9 !px-4 text-xs"
            onClick={onCancel}
            disabled={isStarting}
          >
            {cs.cancel}
          </button>
          <button
            type="button"
            className="btn-primary !min-h-9 !px-4 text-xs"
            onClick={onConfirm}
            disabled={isStarting}
            data-autofocus
          >
            <Play size={13} />
            {isStarting ? cs.starting : cs.startRun}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div>
          <p className="mb-1.5 text-xs font-semibold text-ink-300">{cs.runsOn}</p>
          {targetAgents.length > 0 ? (
            <div className="flex flex-wrap gap-2 rounded-xl border border-ink-800 bg-ink-950/50 p-3">
              {targetAgents.map((a) => (
                <span
                  key={a}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 font-mono text-[11px] font-semibold text-emerald-300"
                >
                  <MonitorDot size={12} /> {a}
                </span>
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-ink-800 bg-ink-950/50 p-3 text-[11px] text-ink-500">
              {cs.runsOnServer}
            </div>
          )}
        </div>

        <div>
          <p className="mb-1.5 text-xs font-semibold text-ink-300">{cs.modeLabel}</p>
          <div className="grid grid-cols-2 gap-2 rounded-xl border border-ink-800 bg-ink-950/50 p-1">
            <ModeButton
              active={mode === "live"}
              onClick={() => onModeChange("live")}
              title={cs.modeLive}
              subtitle={cs.modeLiveSub}
            />
            <ModeButton
              active={mode === "dry_run"}
              onClick={() => onModeChange("dry_run")}
              title={cs.modeDry}
              subtitle={cs.modeDrySub}
            />
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-semibold text-ink-300">
            {cs.execInputs}
          </label>
          <textarea
            value={inputs}
            onChange={(e) => onInputsChange(e.target.value)}
            rows={4}
            className="w-full rounded-lg border border-ink-800 bg-ink-950/60 p-3 font-mono text-xs text-ink-200 outline-none focus:border-accent-300/60"
          />
        </div>

        {error ? <ErrorBanner error={error} /> : null}
      </div>
    </Dialog>
  );
}

function ModeButton({
  active,
  onClick,
  title,
  subtitle,
}: {
  active: boolean;
  onClick: () => void;
  title: string;
  subtitle: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "flex flex-col items-start rounded-lg border p-2.5 text-left transition",
        active
          ? "border-accent-500 bg-accent-500/10 text-accent-200"
          : "border-transparent text-ink-400 hover:text-ink-200",
      ].join(" ")}
    >
      <span className="text-xs font-semibold">{title}</span>
      <span className="mt-0.5 text-[10px] text-ink-500">{subtitle}</span>
    </button>
  );
}
