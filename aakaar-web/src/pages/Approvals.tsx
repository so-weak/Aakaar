import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  Check,
  ClipboardCheck,
  FlaskConical,
  GitBranch,
  Play,
  ShieldQuestion,
  X,
} from "lucide-react";

import { approvals as approvalsApi } from "@/api";
import type { ApprovalRequest, ApprovalStatus } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDateTime } from "@/lib/datetime";

const FILTERS: { value: ApprovalStatus | "all"; label: string }[] = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "all", label: "All" },
];

const STATUS_STYLES: Record<ApprovalStatus, string> = {
  pending: "ring-amber-400/40 text-amber-300",
  approved: "ring-emerald-400/40 text-emerald-300",
  rejected: "ring-rose-400/40 text-rose-300",
  cancelled: "ring-ink-700 text-ink-400",
};

export function ApprovalsPage() {
  const { claims } = useAuth();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const highlightId = searchParams.get("highlight");

  const [filter, setFilter] = useState<ApprovalStatus | "all">("pending");

  const { data, isLoading, error } = useQuery({
    queryKey: ["approvals", filter],
    queryFn: () =>
      approvalsApi.list(filter === "all" ? {} : { status: filter }),
    // Pending decisions are time-sensitive; keep the queue fresh.
    refetchInterval: 8_000,
  });

  const isAdmin = claims?.role === "tenant_admin";
  const items = data ?? [];

  // A just-opened gate (routed here with ?highlight=) may not be in the current
  // filtered page; surface a hint so the maker knows where it went.
  const highlightVisible = useMemo(
    () => (highlightId ? items.some((r) => r.id === highlightId) : true),
    [highlightId, items],
  );

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Approvals"
        subtitle="Maker-checker gates. Sensitive publishes and run-starts wait here until a second admin approves — the maker cannot approve their own request."
        actions={
          <div className="inline-flex rounded-md border border-ink-700 bg-ink-950/60 p-0.5">
            {FILTERS.map((f) => (
              <button
                key={f.value}
                type="button"
                className={[
                  "rounded px-2.5 py-1 text-xs font-semibold uppercase tracking-wider transition",
                  filter === f.value
                    ? "bg-accent-300/15 text-accent-100"
                    : "text-ink-400 hover:text-ink-200",
                ].join(" ")}
                onClick={() => setFilter(f.value)}
              >
                {f.label}
              </button>
            ))}
          </div>
        }
      />

      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        {highlightId && !highlightVisible ? (
          <div className="mb-4 flex items-center gap-2 rounded-control border border-accent-300/30 bg-accent-300/5 px-4 py-2.5 text-xs text-accent-100">
            <ClipboardCheck size={14} className="shrink-0" />
            <span>
              Your request was opened. It may be under a different filter —
              switch to “All” to find it.
            </span>
          </div>
        ) : null}

        {isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : items.length === 0 ? (
          <EmptyState
            title="Nothing here"
            description={
              filter === "pending"
                ? "No pending approvals. Gated publishes and run-starts will appear here for a second admin to decide."
                : `No ${filter === "all" ? "" : filter + " "}approval requests.`
            }
          />
        ) : (
          <ul className="space-y-3">
            {items.map((req) => (
              <ApprovalCard
                key={req.id}
                req={req}
                isAdmin={isAdmin}
                isMaker={claims?.user_id === req.requested_by}
                highlighted={req.id === highlightId}
                onDecided={() =>
                  queryClient.invalidateQueries({ queryKey: ["approvals"] })
                }
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ---------- card ----------------------------------------------------------

function ApprovalCard({
  req,
  isAdmin,
  isMaker,
  highlighted,
  onDecided,
}: {
  req: ApprovalRequest;
  isAdmin: boolean;
  isMaker: boolean;
  highlighted: boolean;
  onDecided: () => void;
}) {
  const [reason, setReason] = useState("");

  const approve = useMutation({
    mutationFn: () => approvalsApi.approve(req.id, reason),
    onSuccess: onDecided,
  });
  const reject = useMutation({
    mutationFn: () => approvalsApi.reject(req.id, reason),
    onSuccess: onDecided,
  });
  const busy = approve.isPending || reject.isPending;
  const decisionError = approve.error ?? reject.error ?? null;

  const subject = describeSubject(req);
  const pending = req.status === "pending";
  // Only a *different* admin may decide; the maker would get a 409, so don't
  // offer the controls to them — show a "waiting" note instead.
  const canDecide = isAdmin && pending && !isMaker;

  return (
    <li
      className={[
        "card p-4",
        highlighted ? "ring-1 ring-accent-300/60" : "",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <subject.Icon size={15} className="shrink-0 text-accent-300" />
            <span className="text-sm font-semibold text-ink-50">
              {subject.title}
            </span>
            <span className={`badge ${STATUS_STYLES[req.status]}`}>
              {req.status}
            </span>
            {subject.dryRun ? (
              <span className="badge ring-signal-cyan/40 text-signal-cyan">
                <FlaskConical size={11} />
                dry run
              </span>
            ) : null}
          </div>
          <div className="mt-1.5 text-xs text-ink-400">{subject.detail}</div>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-500">
            <span>
              Maker{" "}
              <span className="font-mono text-ink-300">
                {req.requested_by.slice(0, 8)}…
              </span>
            </span>
            <span>requested {formatISTDateTime(req.requested_at)}</span>
            {subject.workflowId ? (
              <Link
                to={`/workflows/${subject.workflowId}`}
                className="text-accent-300 hover:text-accent-200"
              >
                view workflow
              </Link>
            ) : null}
          </div>
        </div>
      </div>

      {req.status !== "pending" ? (
        <div className="mt-3 rounded border border-ink-700 bg-ink-950/50 px-3 py-2 text-xs">
          <span className="text-ink-500">
            {req.status === "approved" ? "Approved" : "Rejected"} by{" "}
          </span>
          <span className="font-mono text-ink-300">
            {req.decided_by ? req.decided_by.slice(0, 8) + "…" : "—"}
          </span>
          {req.decided_at ? (
            <span className="text-ink-500"> · {formatISTDateTime(req.decided_at)}</span>
          ) : null}
          {req.reason ? (
            <div className="mt-1 text-ink-300">“{req.reason}”</div>
          ) : null}
        </div>
      ) : null}

      {canDecide ? (
        <div className="mt-3 border-t border-ink-700/70 pt-3">
          <input
            type="text"
            className="input text-xs"
            placeholder="Decision note (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            maxLength={2000}
          />
          <div className="mt-2.5 flex items-center gap-2">
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                approve.reset();
                reject.reset();
                approve.mutate();
              }}
              disabled={busy}
              title="Approve and perform the gated action"
            >
              <Check size={15} />
              {approve.isPending ? "Approving…" : "Approve"}
            </button>
            <button
              type="button"
              className="btn-ghost text-rose-300 hover:bg-rose-500/10"
              onClick={() => {
                approve.reset();
                reject.reset();
                reject.mutate();
              }}
              disabled={busy}
            >
              <X size={15} />
              {reject.isPending ? "Rejecting…" : "Reject"}
            </button>
            <span className="text-[11px] text-ink-500">
              Approving runs the action under your authorization, attributed to
              the maker.
            </span>
          </div>
          {decisionError ? (
            <div className="mt-2">
              <ErrorBanner error={decisionError} />
            </div>
          ) : null}
        </div>
      ) : pending ? (
        <div className="mt-3 flex items-center gap-1.5 border-t border-ink-700/70 pt-3 text-[11px] text-ink-500">
          <ShieldQuestion size={12} />
          {isMaker
            ? "Waiting for a different admin to decide — you can't approve your own request."
            : "Awaiting a tenant admin's decision."}
        </div>
      ) : null}
    </li>
  );
}

// Translate an approval's subject + frozen context snapshot into display bits.
// subject_ref is the workflow id for both gate kinds today.
function describeSubject(req: ApprovalRequest): {
  Icon: typeof Play;
  title: string;
  detail: string;
  workflowId: string | null;
  dryRun: boolean;
} {
  const ctx = req.context ?? {};
  const name = typeof ctx.workflow_name === "string" ? ctx.workflow_name : null;
  const version = typeof ctx.version === "number" ? ctx.version : null;
  const workflowId =
    typeof ctx.workflow_id === "string" ? ctx.workflow_id : req.subject_ref || null;

  if (req.subject_type === "run_start") {
    return {
      Icon: Play,
      title: "Start run",
      detail: `${name ?? "workflow"}${version != null ? ` · v${version}` : ""}`,
      workflowId,
      dryRun: ctx.mode === "dry_run",
    };
  }
  if (req.subject_type === "workflow_publish") {
    return {
      Icon: GitBranch,
      title: "Publish workflow version",
      detail: `${name ?? "workflow"}${version != null ? ` · v${version}` : ""}`,
      workflowId,
      dryRun: false,
    };
  }
  // Unknown subject types still render — show the raw type so nothing is hidden.
  return {
    Icon: ShieldQuestion,
    title: req.subject_type,
    detail: req.subject_ref,
    workflowId,
    dryRun: false,
  };
}
