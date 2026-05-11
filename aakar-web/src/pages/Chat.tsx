import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import {
  CheckCircle2,
  Clock,
  HelpCircle,
  MessageSquare,
  Plus,
  Save,
  Send,
  ShieldAlert,
  Trash2,
} from "lucide-react";

import { chatSessions as sessionsApi } from "@/api";
import type { ChatSession, RawChatResponse } from "@/api/types";
import { DagViewer } from "@/components/DagViewer";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

export function ChatPage() {
  const { id: routeId } = useParams<{ id?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const sessionsQ = useQuery({
    queryKey: ["chat-sessions"],
    queryFn: sessionsApi.list,
  });

  // Auto-pick the most-recent session if none selected.
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
    <div className="flex h-full flex-col">
      <PageHeader
        title="Chat"
        subtitle="Iterate on a workflow with the planner. Sessions persist across reloads."
      />
      <div className="grid flex-1 grid-cols-[14rem_1fr] overflow-hidden">
        <SessionList
          sessions={sessionsQ.data ?? []}
          activeId={activeId}
          onPick={(id) => navigate(`/chat/${id}`)}
          onNew={() => create.mutate(undefined)}
          isCreating={create.isPending}
        />
        {activeId ? (
          <SessionPane key={activeId} sessionId={activeId} />
        ) : (
          <div className="grid place-items-center text-sm text-ink-500">
            {sessionsQ.isLoading ? "Loading sessions…" : "No active session."}
          </div>
        )}
      </div>
    </div>
  );
}

function SessionList({
  sessions,
  activeId,
  onPick,
  onNew,
  isCreating,
}: {
  sessions: { id: string; title: string; is_dirty: boolean; workflow_id: string | null }[];
  activeId: string | null;
  onPick: (id: string) => void;
  onNew: () => void;
  isCreating: boolean;
}) {
  return (
    <aside className="flex flex-col border-r border-ink-800 bg-ink-900/30">
      <div className="flex items-center justify-between gap-2 border-b border-ink-800 px-3 py-2">
        <span className="panel-title">Sessions</span>
        <button
          type="button"
          className="btn-ghost text-xs"
          onClick={onNew}
          disabled={isCreating}
          title="New session"
        >
          <Plus size={14} /> New
        </button>
      </div>
      <ul className="flex-1 overflow-y-auto p-2">
        {sessions.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              onClick={() => onPick(s.id)}
              className={[
                "block w-full rounded-md border px-3 py-2 text-left text-sm transition",
                s.id === activeId
                  ? "border-accent-300/60 bg-accent-300/10 text-accent-100"
                  : "border-transparent text-ink-300 hover:border-ink-700 hover:bg-ink-900/60",
              ].join(" ")}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-medium">{s.title}</span>
                {s.is_dirty ? (
                  <span className="badge ring-amber-400/40 text-amber-300">dirty</span>
                ) : s.workflow_id ? (
                  <span className="badge ring-emerald-400/40 text-emerald-300">saved</span>
                ) : null}
              </div>
            </button>
          </li>
        ))}
        {sessions.length === 0 ? (
          <li className="px-3 py-4 text-xs text-ink-500">No sessions yet.</li>
        ) : null}
      </ul>
    </aside>
  );
}

function SessionPane({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const [input, setInput] = useState("");
  const [confirmingSave, setConfirmingSave] = useState(false);
  const [name, setName] = useState("");

  const sessionQ = useQuery({
    queryKey: ["chat-session", sessionId],
    queryFn: () => sessionsApi.get(sessionId),
  });

  const send = useMutation({
    mutationFn: (message: string) => sessionsApi.send(sessionId, { message }),
    onSuccess: (s) => {
      queryClient.setQueryData(["chat-session", sessionId], s);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
  });

  const save = useMutation({
    mutationFn: (input: { name?: string; confirm?: boolean }) =>
      sessionsApi.save(sessionId, input),
    onSuccess: () => {
      setConfirmingSave(false);
      queryClient.invalidateQueries({ queryKey: ["chat-session", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  const remove = useMutation({
    mutationFn: () => sessionsApi.remove(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
  });

  const session = sessionQ.data;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput("");
    send.mutate(text);
  };

  if (sessionQ.isLoading) {
    return <div className="grid place-items-center text-sm text-ink-500">Loading…</div>;
  }
  if (sessionQ.error || !session) {
    return (
      <div className="p-7">
        <ErrorBanner error={sessionQ.error ?? "session not found"} />
      </div>
    );
  }

  return (
    <div className="grid h-full grid-cols-2 overflow-hidden">
      {/* Conversation column */}
      <section className="flex h-full flex-col border-r border-ink-800">
        <div className="flex flex-1 flex-col gap-4 overflow-y-auto px-6 py-6">
          {session.messages.length === 0 ? (
            <div className="text-sm text-ink-400">
              <p>Try something like:</p>
              <ul className="mt-2 list-disc space-y-1 pl-5">
                <li>"Open my HDFC dashboard and download last month's statement."</li>
                <li>"Log in to example.test with primary account; captcha image is img.captcha; captcha input is input[name='captcha']."</li>
                <li>"Wait 30 seconds, then take a screenshot of example.com."</li>
              </ul>
            </div>
          ) : null}

          {session.messages.map((m) =>
            m.role === "user" ? (
              <UserBubble key={m.id} text={m.text} />
            ) : (
              <PlannerBubble key={m.id} response={m.payload as RawChatResponse} />
            ),
          )}

          {send.isPending ? (
            <div className="text-sm italic text-ink-400">Planner is thinking…</div>
          ) : null}
          {send.error ? <ErrorBanner error={send.error} /> : null}
        </div>

        <form
          onSubmit={onSubmit}
          className="flex items-end gap-2 border-t border-ink-800 p-4"
        >
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                onSubmit(e as unknown as FormEvent);
              }
            }}
            placeholder="Describe a workflow or refine the current draft… (⌘+Enter)"
            rows={2}
            className="input resize-none"
          />
          <button type="submit" className="btn-primary" disabled={send.isPending}>
            <Send size={15} /> Send
          </button>
        </form>
      </section>

      {/* Draft column */}
      <section className="flex h-full flex-col">
        <DraftHeader
          session={session}
          name={name}
          setName={setName}
          onSave={() => {
            // First save needs a name; updates need confirm.
            if (session.workflow_id == null) {
              if (!name.trim()) {
                setConfirmingSave(true);
                return;
              }
              save.mutate({ name: name.trim() });
            } else {
              setConfirmingSave(true);
            }
          }}
          isSaving={save.isPending}
          onDelete={() => {
            if (window.confirm("Delete this chat session? Messages and draft will be lost.")) {
              remove.mutate();
            }
          }}
        />
        {save.error ? (
          <div className="border-b border-ink-800 p-3">
            <ErrorBanner error={save.error} />
          </div>
        ) : null}
        <div className="flex-1 overflow-hidden">
          {session.draft_dag ? (
            <DagViewer dag={session.draft_dag} />
          ) : (
            <div className="grid h-full place-items-center px-6 text-center text-sm text-ink-500">
              <div>
                <MessageSquare size={28} className="mx-auto text-ink-700" />
                <p className="mt-3">The DAG will appear here once the planner replies.</p>
              </div>
            </div>
          )}
        </div>
      </section>

      {confirmingSave ? (
        <SaveConfirmModal
          session={session}
          name={name}
          setName={setName}
          onCancel={() => setConfirmingSave(false)}
          onConfirm={() => {
            if (session.workflow_id == null) {
              save.mutate({ name: name.trim() });
            } else {
              save.mutate({ confirm: true });
            }
          }}
          isSaving={save.isPending}
        />
      ) : null}
    </div>
  );
}

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
  const isFirstSave = session.workflow_id == null;
  const canSave = session.draft_dag != null && (isFirstSave || session.is_dirty);
  const buttonLabel = isFirstSave
    ? "Save workflow"
    : session.is_dirty
      ? `Update workflow (v${(session.saved_version ?? 0) + 1})`
      : `Saved · v${session.saved_version}`;

  return (
    <div className="flex items-center justify-between gap-3 border-b border-ink-800 px-5 py-3">
      <div className="flex flex-1 items-center gap-3">
        {isFirstSave ? (
          <input
            className="input max-w-xs"
            placeholder="Workflow name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        ) : (
          <div className="flex items-center gap-2 font-mono text-xs text-ink-400">
            <Clock size={13} />
            <span>v{session.saved_version}</span>
            {session.is_dirty ? (
              <span className="badge ring-amber-400/40 text-amber-300">drift</span>
            ) : null}
          </div>
        )}
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn-ghost text-rose-300 hover:bg-rose-500/10"
          onClick={onDelete}
          title="Delete session"
        >
          <Trash2 size={14} />
        </button>
        <button
          type="button"
          className="btn-primary"
          onClick={onSave}
          disabled={!canSave || isSaving}
        >
          <Save size={15} />
          {buttonLabel}
        </button>
      </div>
    </div>
  );
}

function SaveConfirmModal({
  session,
  name,
  setName,
  onCancel,
  onConfirm,
  isSaving,
}: {
  session: ChatSession;
  name: string;
  setName: (v: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
  isSaving: boolean;
}) {
  const isFirstSave = session.workflow_id == null;
  const dag = session.draft_dag;
  const nodeCount = dag?.nodes.length ?? 0;
  const edgeCount = dag?.edges.length ?? 0;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 backdrop-blur">
      <div className="card w-full max-w-md p-5">
        <h3 className="mb-2 text-base font-semibold text-ink-50">
          {isFirstSave ? "Save workflow" : "Update saved workflow"}
        </h3>
        <p className="text-sm text-ink-300">
          {isFirstSave
            ? "Persist the current draft as a new workflow. Future runs will execute this DAG."
            : `This will write a new version (v${(session.saved_version ?? 0) + 1}) of the saved workflow. Existing runs of older versions are unaffected.`}
        </p>
        <div className="mt-3 rounded-md border border-ink-800 bg-ink-900/40 px-3 py-2 text-xs text-ink-400">
          <div>
            {nodeCount} node{nodeCount === 1 ? "" : "s"}, {edgeCount} edge
            {edgeCount === 1 ? "" : "s"}
          </div>
          {session.draft_rationale ? (
            <div className="mt-1 italic text-ink-300">{session.draft_rationale}</div>
          ) : null}
        </div>
        {isFirstSave ? (
          <label className="mt-4 block">
            <span className="panel-title">Name</span>
            <input
              className="input mt-1"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Daily settlement download"
              autoFocus
            />
          </label>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onCancel} disabled={isSaving}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={onConfirm}
            disabled={isSaving || (isFirstSave && !name.trim())}
          >
            <Save size={15} />
            {isSaving ? "Saving…" : isFirstSave ? "Save" : "Confirm update"}
          </button>
        </div>
      </div>
    </div>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap break-words [overflow-wrap:anywhere] rounded-lg bg-accent-500/10 px-3.5 py-2 text-sm text-ink-100 ring-1 ring-inset ring-accent-500/30">
        {text}
      </div>
    </div>
  );
}

function PlannerBubble({ response }: { response: RawChatResponse }) {
  if (response.kind === "dag" && response.dag) {
    const dag = response.dag;
    return (
      <div className="flex justify-start">
        <div className="card max-w-[85%] px-4 py-3 [overflow-wrap:anywhere]">
          <div className="mb-2 flex items-center gap-2 text-xs font-medium text-emerald-300">
            <CheckCircle2 size={14} /> Drafted a workflow
          </div>
          <p className="break-words text-sm text-ink-100">{response.rationale}</p>
          <p className="mt-2 text-xs text-ink-500">
            {dag.nodes.length} node{dag.nodes.length === 1 ? "" : "s"} · preview on the right.
          </p>
        </div>
      </div>
    );
  }
  if (response.kind === "clarify") {
    return (
      <div className="flex justify-start">
        <div className="card max-w-[85%] px-4 py-3 [overflow-wrap:anywhere]">
          <div className="mb-2 flex items-center gap-2 text-xs font-medium text-amber-300">
            <HelpCircle size={14} /> Need a bit more info
          </div>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink-100">
            {response.questions.map((q, i) => (
              <li key={i} className="break-words">{q}</li>
            ))}
          </ul>
        </div>
      </div>
    );
  }
  return (
    <div className="flex justify-start">
      <div className="card max-w-[85%] px-4 py-3 [overflow-wrap:anywhere]">
        <div className="mb-2 flex items-center gap-2 text-xs font-medium text-rose-300">
          <ShieldAlert size={14} /> Capability not granted
        </div>
        <p className="break-words text-sm text-ink-100">{response.explanation}</p>
        <div className="mt-2 text-xs text-ink-400">
          Ask your tenant admin to grant:{" "}
          {response.needed.map((ref, i) => (
            <span key={ref}>
              <code className="rounded bg-ink-800 px-1.5 py-0.5 font-mono text-xs text-ink-200">
                {ref}
              </code>
              {i < response.needed.length - 1 ? ", " : ""}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
