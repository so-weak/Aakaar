import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Apple,
  CheckCircle2,
  Copy,
  Monitor,
  MonitorSmartphone,
  Plus,
  Server,
  Terminal,
  Trash2,
  X,
} from "lucide-react";

import { agents as agentsApi } from "@/api";
import type { AgentEnrollResponse, RemoteAgent } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDateTime } from "@/lib/datetime";

const AGENTS_KEY = ["agents"];

export function AgentsPage() {
  const queryClient = useQueryClient();
  const agentsQ = useQuery({ queryKey: AGENTS_KEY, queryFn: agentsApi.list });

  const [enrolling, setEnrolling] = useState(false);

  const revoke = useMutation({
    mutationFn: (id: string) => agentsApi.revoke(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: AGENTS_KEY }),
  });

  const items = agentsQ.data ?? [];

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Agents"
        subtitle="Remote workstations enrolled to run capabilities outside the API host — desktop automations, GUI-bound steps, and on-prem reach."
        actions={
          <button
            type="button"
            className="btn-primary"
            onClick={() => setEnrolling(true)}
            data-tour="agents-enroll"
          >
            <Plus size={15} />
            Enroll agent
          </button>
        }
      />

      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        {agentsQ.isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : agentsQ.error ? (
          <ErrorBanner error={agentsQ.error} />
        ) : items.length === 0 ? (
          <EmptyState
            title="No agents enrolled"
            description="Enroll a workstation to route DAG nodes onto a remote machine. The one-time enrollment key is shown once at creation."
            action={
              <button
                type="button"
                className="btn-primary"
                onClick={() => setEnrolling(true)}
              >
                <Plus size={15} />
                Enroll agent
              </button>
            }
          />
        ) : (
          <>
            {revoke.error ? (
              <div className="mb-4">
                <ErrorBanner error={revoke.error} />
              </div>
            ) : null}
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Agent</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">OS</th>
                  <th className="px-3 py-2 font-medium">GUI</th>
                  <th className="px-3 py-2 font-medium">Pools</th>
                  <th className="px-3 py-2 font-medium">Capabilities</th>
                  <th className="px-3 py-2 font-medium">Version</th>
                  <th className="px-3 py-2 font-medium">Last seen</th>
                  <th className="px-3 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {items.map((agent) => (
                  <AgentRow
                    key={agent.id}
                    agent={agent}
                    onRevoke={() => {
                      if (
                        window.confirm(
                          `Revoke "${agent.alias}"? Its enrollment is invalidated and it can no longer pick up work until re-enrolled.`,
                        )
                      ) {
                        revoke.mutate(agent.id);
                      }
                    }}
                    revoking={revoke.isPending}
                  />
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>

      {enrolling ? <EnrollModal onClose={() => setEnrolling(false)} /> : null}
    </div>
  );
}

// ---------- table row ------------------------------------------------------

function AgentRow({
  agent,
  onRevoke,
  revoking,
}: {
  agent: RemoteAgent;
  onRevoke: () => void;
  revoking: boolean;
}) {
  return (
    <tr className="align-top">
      <td className="px-3 py-2.5">
        <div className="font-medium text-ink-100">{agent.alias}</div>
        {agent.hostname ? (
          <div className="mt-0.5 font-mono text-[11px] text-ink-500">
            {agent.hostname}
          </div>
        ) : null}
      </td>
      <td className="px-3 py-2.5">
        <StatusChip online={agent.online} status={agent.status} />
      </td>
      <td className="px-3 py-2.5 text-ink-300">
        <span className="flex items-center gap-1.5">
          <OsIcon os={agent.os} />
          {osLabel(agent.os)}
        </span>
      </td>
      <td className="px-3 py-2.5">
        {agent.gui_capable ? (
          <span className="badge ring-accent-500/40 text-accent-300">GUI</span>
        ) : (
          <span className="text-ink-600">—</span>
        )}
      </td>
      <td className="px-3 py-2.5">
        {agent.pools.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {agent.pools.map((p) => (
              <span key={p} className="badge ring-ink-700 text-ink-300">
                {p}
              </span>
            ))}
          </span>
        ) : (
          <span className="text-ink-600">—</span>
        )}
      </td>
      <td className="px-3 py-2.5 text-ink-300">
        <span className="font-mono text-xs">{agent.capabilities.length}</span>
      </td>
      <td className="px-3 py-2.5 font-mono text-xs text-ink-400">
        {agent.agent_version ?? "—"}
      </td>
      <td className="px-3 py-2.5 whitespace-nowrap text-ink-400">
        {formatISTDateTime(agent.last_seen)}
      </td>
      <td className="px-3 py-2.5 text-right">
        <button
          type="button"
          className="btn-ghost text-rose-300 hover:bg-rose-500/10"
          onClick={onRevoke}
          disabled={revoking}
          title="Revoke this agent"
        >
          <Trash2 size={14} />
        </button>
      </td>
    </tr>
  );
}

// Reuses the run-status chip vocabulary (ring + text token pairs).
function StatusChip({ online, status }: { online: boolean; status: string }) {
  return (
    <span
      className={
        online
          ? "badge ring-emerald-400/40 text-emerald-300"
          : "badge ring-ink-700 text-ink-400"
      }
    >
      {online ? "online" : status || "offline"}
    </span>
  );
}

// ---------- OS helpers -----------------------------------------------------

function osLabel(os: string | null): string {
  if (!os) return "unknown";
  const key = os.toLowerCase();
  if (key.includes("win")) return "Windows";
  if (key.includes("mac") || key.includes("darwin") || key.includes("osx"))
    return "macOS";
  if (key.includes("linux")) return "Linux";
  return os;
}

function OsIcon({ os }: { os: string | null }) {
  const key = (os ?? "").toLowerCase();
  if (key.includes("mac") || key.includes("darwin") || key.includes("osx")) {
    return <Apple size={14} className="text-ink-400" />;
  }
  if (key.includes("linux")) {
    return <Terminal size={14} className="text-ink-400" />;
  }
  if (key.includes("win")) {
    return <Monitor size={14} className="text-ink-400" />;
  }
  return <Server size={14} className="text-ink-500" />;
}

// ---------- enroll modal ---------------------------------------------------

function EnrollModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [alias, setAlias] = useState("");
  const [poolsText, setPoolsText] = useState("");
  const [result, setResult] = useState<AgentEnrollResponse | null>(null);

  const enroll = useMutation({
    mutationFn: () =>
      agentsApi.enroll({
        alias: alias.trim(),
        pools: parsePools(poolsText),
      }),
    onSuccess: (res) => {
      setResult(res);
      queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!alias.trim() || enroll.isPending) return;
    enroll.mutate();
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 backdrop-blur">
      <div className="card w-full max-w-lg p-5">
        <div className="mb-1 flex items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-base font-semibold text-ink-50">
            <MonitorSmartphone size={16} className="text-accent-300" />
            {result ? "Agent enrolled" : "Enroll an agent"}
          </h3>
          <button
            type="button"
            className="btn-ghost !min-h-0 !px-2 !py-1"
            onClick={onClose}
            aria-label="Close"
          >
            <X size={15} />
          </button>
        </div>

        {result ? (
          <EnrollSuccess result={result} onDone={onClose} />
        ) : (
          <form onSubmit={onSubmit} className="space-y-3">
            <p className="text-xs text-ink-400">
              Give the workstation an alias and any pool labels it should join.
              The one-time enrollment key is shown only on the next screen.
            </p>
            <label className="block">
              <span className="panel-title">Alias</span>
              <input
                className="input mt-1"
                type="text"
                value={alias}
                onChange={(e) => setAlias(e.target.value)}
                placeholder="e.g. ops-laptop-01"
                spellCheck={false}
                required
              />
            </label>
            <label className="block">
              <span className="panel-title">Pools</span>
              <input
                className="input mt-1 font-mono text-xs"
                type="text"
                value={poolsText}
                onChange={(e) => setPoolsText(e.target.value)}
                placeholder="comma-separated, e.g. desktop, mumbai"
                spellCheck={false}
              />
              <span className="mt-1 block text-[11px] text-ink-500">
                Optional. Pools let DAG nodes target a group of agents instead of
                a single alias.
              </span>
            </label>
            {enroll.error ? <ErrorBanner error={enroll.error} /> : null}
            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                className="btn-ghost"
                onClick={onClose}
                disabled={enroll.isPending}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="btn-primary"
                disabled={enroll.isPending || !alias.trim()}
              >
                <Plus size={14} />
                {enroll.isPending ? "Enrolling…" : "Enroll"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

function EnrollSuccess({
  result,
  onDone,
}: {
  result: AgentEnrollResponse;
  onDone: () => void;
}) {
  const [copiedField, setCopiedField] = useState<"key" | "id" | null>(null);

  const copy = async (value: string, field: "key" | "id") => {
    try {
      await navigator.clipboard.writeText(value);
      setCopiedField(field);
      window.setTimeout(() => setCopiedField(null), 2000);
    } catch {
      // Clipboard may be unavailable (insecure context); the value is still
      // selectable in its field.
      setCopiedField(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="brand-shadow-pink-sm flex items-start gap-2 rounded-control border border-amber-300/35 bg-amber-950/40 px-3 py-2 text-sm text-amber-100">
        <AlertOnce />
        <span>
          Copy the enrollment key and agent id now — they are shown once here
          and cannot be retrieved later.
        </span>
      </div>

      <div>
        <span className="panel-title">Enrollment key</span>
        <div className="mt-1 flex items-stretch gap-2">
          <input
            readOnly
            value={result.enrollment_key}
            onFocus={(e) => e.currentTarget.select()}
            className="input flex-1 font-mono text-xs"
            aria-label="Enrollment key"
          />
          <button
            type="button"
            className="btn-ghost shrink-0"
            onClick={() => copy(result.enrollment_key, "key")}
            title="Copy enrollment key"
          >
            {copiedField === "key" ? (
              <CheckCircle2 size={14} className="text-emerald-300" />
            ) : (
              <Copy size={14} />
            )}
            {copiedField === "key" ? "Copied" : "Copy"}
          </button>
        </div>
      </div>

      <div>
        <span className="panel-title">Agent id</span>
        <div className="mt-1 flex items-stretch gap-2">
          <input
            readOnly
            value={result.agent_id}
            onFocus={(e) => e.currentTarget.select()}
            className="input flex-1 font-mono text-xs"
            aria-label="Agent id"
          />
          <button
            type="button"
            className="btn-ghost shrink-0"
            onClick={() => copy(result.agent_id, "id")}
            title="Copy agent id"
          >
            {copiedField === "id" ? (
              <CheckCircle2 size={14} className="text-emerald-300" />
            ) : (
              <Copy size={14} />
            )}
            {copiedField === "id" ? "Copied" : "Copy"}
          </button>
        </div>
      </div>

      <div className="card bg-ink-900/40 p-3 text-xs text-ink-300">
        <div className="panel-title mb-2">Install the agent</div>
        <p className="mb-2 text-ink-400">
          Install the agent on the workstation, then connect it to this server
          with the key above (the workstation needs no inbound ports):
        </p>
        <code className="block rounded-control bg-ink-950/60 px-2 py-1.5 font-mono text-[11px] text-accent-200">
          aakaar-agent --server &lt;server-url&gt; --key &lt;key&gt;
        </code>
        <ul className="mt-2 space-y-1.5">
          <li className="flex items-start gap-2">
            <Monitor size={13} className="mt-0.5 shrink-0 text-ink-500" />
            <span>
              <span className="text-ink-100">Windows</span> — install the agent on
              the workstation, then run the command above.
            </span>
          </li>
          <li className="flex items-start gap-2">
            <Apple size={13} className="mt-0.5 shrink-0 text-ink-500" />
            <span>
              <span className="text-ink-100">macOS</span> — install the agent
              (Python ≥ 3.11), then run the command above.
            </span>
          </li>
          <li className="flex items-start gap-2">
            <Terminal size={13} className="mt-0.5 shrink-0 text-ink-500" />
            <span>
              <span className="text-ink-100">Linux</span> — install the agent, then
              run it unattended via a systemd service.
            </span>
          </li>
        </ul>
        <div className="mt-2 text-[11px] text-ink-500">
          Per-OS setup and running as a service — see the agent README.
        </div>
      </div>

      <div className="flex justify-end">
        <button type="button" className="btn-primary" onClick={onDone}>
          <CheckCircle2 size={14} />
          Done
        </button>
      </div>
    </div>
  );
}

// Small dedicated warning glyph so we don't import AlertTriangle twice.
function AlertOnce() {
  return (
    <span
      aria-hidden="true"
      className="mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full bg-amber-300/25 text-[10px] font-bold text-amber-200"
    >
      1
    </span>
  );
}

// "desktop, mumbai" -> ["desktop", "mumbai"] (trimmed, de-duped, non-empty).
function parsePools(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(",")) {
    const p = raw.trim();
    if (p && !seen.has(p)) {
      seen.add(p);
      out.push(p);
    }
  }
  return out;
}
