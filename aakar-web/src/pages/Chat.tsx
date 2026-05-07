import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, HelpCircle, MessageSquare, Save, Send, ShieldAlert, Wand2 } from "lucide-react";

import { chat as chatApi, workflows as workflowsApi } from "@/api";
import type { Dag, RawChatResponse } from "@/api/types";
import { DagViewer } from "@/components/DagViewer";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";

// Each chat turn carries either the user's message or the planner's response.
type Turn =
  | { role: "user"; text: string; id: number }
  | { role: "planner"; response: RawChatResponse; id: number };

export function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [name, setName] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  // Latest planner DAG, if any. Each turn replaces the prior one — workflows
  // are full replacements, not incremental edits.
  const latestDag: Dag | null = (() => {
    for (let i = turns.length - 1; i >= 0; i--) {
      const t = turns[i];
      if (t.role === "planner" && t.response.kind === "dag") return t.response.dag;
    }
    return null;
  })();

  const send = useMutation({
    mutationFn: (message: string) =>
      chatApi.send({ message, current_dag: latestDag }),
    onSuccess: (response) => {
      setTurns((prev) => [...prev, { role: "planner", id: prev.length, response }]);
    },
  });

  const save = useMutation({
    mutationFn: () => {
      if (!latestDag) throw new Error("no DAG to save");
      const lastDagTurn = [...turns]
        .reverse()
        .find((t): t is Extract<Turn, { role: "planner" }> => t.role === "planner");
      const rationale =
        lastDagTurn?.response.kind === "dag" ? lastDagTurn.response.rationale : "";
      return workflowsApi.create({
        name: name.trim() || "Untitled workflow",
        description: "",
        dag: latestDag,
        rationale,
      });
    },
    onSuccess: (workflow) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${workflow.id}`);
    },
    onError: (err) => {
      setSaveError(err instanceof Error ? err.message : String(err));
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setTurns((prev) => [...prev, { role: "user", id: prev.length, text }]);
    setInput("");
    send.mutate(text);
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Chat"
        subtitle="Describe what you want; the planner will draft a workflow."
      />

      <div className="relative z-10 grid flex-1 grid-cols-2 overflow-hidden">
        {/* Chat column */}
        <section className="flex h-full flex-col border-r border-ink-700/80 bg-ink-950/30">
          <div className="flex-1 space-y-4 overflow-y-auto px-6 py-6">
            {turns.length === 0 ? (
              <div className="card relative overflow-hidden p-5 text-sm text-ink-300">
                <div className="absolute right-4 top-4 text-accent-300/40">
                  <Wand2 size={32} />
                </div>
                <div className="stamp mb-4">prompt tape</div>
                <p className="max-w-md text-base font-semibold text-ink-100">
                  Describe the outcome. Aakar will turn it into a workflow graph.
                </p>
                <div className="mt-4 grid gap-2">
                  {[
                    "Open my HDFC dashboard and download last month's statement.",
                    "Wait 30 seconds, then take a screenshot of example.com.",
                    "Hit the public weather API for Mumbai and save the JSON.",
                  ].map((example) => (
                    <button
                      key={example}
                      type="button"
                      className="rounded-md border border-ink-700 bg-ink-950/45 px-3 py-2 text-left text-xs text-ink-300 transition hover:border-accent-300/60 hover:text-ink-50"
                      onClick={() => setInput(example)}
                    >
                      “{example}”
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            {turns.map((turn) =>
              turn.role === "user" ? (
                <UserMessage key={turn.id} text={turn.text} />
              ) : (
                <PlannerMessage key={turn.id} response={turn.response} />
              ),
            )}

            {send.isPending ? (
              <div className="font-mono text-xs uppercase tracking-[0.18em] text-accent-200">
                Planner is thinking...
              </div>
            ) : null}
            {send.error ? <ErrorBanner error={send.error} /> : null}
          </div>

          <form
            onSubmit={onSubmit}
            className="flex items-end gap-3 border-t border-ink-700/80 bg-ink-950/60 p-4 backdrop-blur"
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
              placeholder="Describe a workflow...  (⌘+Enter to send)"
              rows={2}
              className="input resize-none"
            />
            <button type="submit" className="btn-primary" disabled={send.isPending}>
              <Send size={15} /> Send
            </button>
          </form>
        </section>

        {/* DAG column */}
        <section className="flex h-full flex-col bg-ink-900/18">
          {latestDag ? (
            <>
              <div className="flex items-center justify-between gap-3 border-b border-ink-700/80 bg-ink-950/45 px-5 py-3 backdrop-blur">
                <input
                  className="input max-w-xs"
                  placeholder="Workflow name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => {
                    setSaveError(null);
                    save.mutate();
                  }}
                  disabled={save.isPending}
                >
                  <Save size={15} />
                  Save workflow
                </button>
              </div>
              {saveError ? (
                <div className="border-b border-ink-700/80 p-3">
                  <ErrorBanner error={saveError} />
                </div>
              ) : null}
              <div className="flex-1">
                <DagViewer dag={latestDag} />
              </div>
            </>
          ) : (
            <div className="grid h-full place-items-center px-6 text-center text-sm text-ink-500">
              <div className="card max-w-sm px-8 py-7">
                <MessageSquare size={30} className="mx-auto text-signal-cyan" />
                <p className="mt-4 font-mono text-xs uppercase tracking-[0.2em] text-ink-300">
                  DAG preview channel
                </p>
                <p className="mt-3 leading-6">The graph appears here once the planner replies.</p>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-lg border border-accent-300/45 bg-accent-300/12 px-3.5 py-2 text-sm font-medium text-ink-50 shadow-[4px_4px_0_rgb(255_59_147/0.2)]">
        {text}
      </div>
    </div>
  );
}

function PlannerMessage({ response }: { response: RawChatResponse }) {
  if (response.kind === "dag" && response.dag) {
    const dag = response.dag;
    return (
      <div className="flex justify-start">
        <div className="card max-w-[85%] px-4 py-3">
          <div className="mb-2 flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-wide text-emerald-300">
            <CheckCircle2 size={14} /> Drafted a workflow
          </div>
          <p className="text-sm text-ink-100">{response.rationale}</p>
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
        <div className="card max-w-[85%] px-4 py-3">
          <div className="mb-2 flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-wide text-amber-300">
            <HelpCircle size={14} /> Need a bit more info
          </div>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink-100">
            {response.questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </div>
      </div>
    );
  }
  return (
    <div className="flex justify-start">
      <div className="card max-w-[85%] px-4 py-3">
        <div className="mb-2 flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-wide text-rose-300">
          <ShieldAlert size={14} /> Capability not granted
        </div>
        <p className="text-sm text-ink-100">{response.explanation}</p>
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
