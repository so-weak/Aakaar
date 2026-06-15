import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Download,
  Search,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";

import { audit as auditApi } from "@/api";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDateTime } from "@/lib/datetime";

const PAGE_SIZE = 25;

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function AuditLogPage() {
  // `filter` is the committed prefix used for the query; `filterDraft` holds
  // the in-flight text so typing doesn't refetch on every keystroke.
  const [filterDraft, setFilterDraft] = useState("");
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ["audit", filter, offset],
    queryFn: () =>
      auditApi.list({
        limit: PAGE_SIZE,
        offset,
        action_prefix: filter || undefined,
      }),
    placeholderData: (prev) => prev,
  });

  // Tamper-evidence: recompute the tenant's audit hash chain on demand. The
  // result is kept until the next verify so the operator can read it.
  const verify = useMutation({
    mutationFn: () => auditApi.verify(),
  });

  // Stream the chain as JSONL for offline re-verification by an external
  // auditor (fetched with the bearer token, then handed to the browser).
  const exportLog = useMutation({
    mutationFn: () => auditApi.exportBlob(),
    onSuccess: (blob) =>
      triggerDownload(blob, `audit-${new Date().toISOString().slice(0, 10)}.jsonl`),
  });

  const entries = data?.entries ?? [];
  const total = data?.total ?? 0;
  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + PAGE_SIZE, total);
  const canPrev = offset > 0;
  const canNext = offset + PAGE_SIZE < total;

  const onApplyFilter = (e: FormEvent) => {
    e.preventDefault();
    setOffset(0);
    setFilter(filterDraft.trim());
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Audit log"
        subtitle="Every privileged action in your tenant — who did what, when, and to which target. The trail is a tamper-evident hash chain you can verify and export."
        actions={
          <div className="flex items-center gap-2">
            <form onSubmit={onApplyFilter} className="flex items-center gap-2">
              <div className="relative">
                <Search
                  size={14}
                  className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-500"
                />
                <input
                  type="search"
                  value={filterDraft}
                  onChange={(e) => setFilterDraft(e.target.value)}
                  placeholder="Filter by action prefix"
                  aria-label="Filter by action prefix"
                  className="input !py-1.5 !pl-8 text-xs"
                />
              </div>
              <button type="submit" className="btn-ghost" disabled={isFetching}>
                Apply
              </button>
            </form>
            <button
              type="button"
              className="btn-ghost"
              onClick={() => verify.mutate()}
              disabled={verify.isPending}
              title="Recompute the audit hash chain end-to-end"
            >
              <ShieldCheck size={15} />
              {verify.isPending ? "Verifying…" : "Verify integrity"}
            </button>
            <button
              type="button"
              className="btn-ghost"
              onClick={() => exportLog.mutate()}
              disabled={exportLog.isPending}
              title="Download the chained audit log as JSONL for external re-verification"
            >
              <Download size={15} />
              {exportLog.isPending ? "Exporting…" : "Export"}
            </button>
          </div>
        }
      />

      {verify.data ? (
        verify.data.ok ? (
          <div className="flex items-center gap-2 border-b border-emerald-400/25 bg-emerald-950/30 px-7 py-2.5 text-xs text-emerald-200">
            <CheckCircle2 size={14} className="shrink-0" />
            <span>
              Audit chain intact — {verify.data.entries_checked} entr
              {verify.data.entries_checked === 1 ? "y" : "ies"} verified
              {verify.data.first_seq != null && verify.data.last_seq != null
                ? ` (seq ${verify.data.first_seq}–${verify.data.last_seq})`
                : ""}
              . No tampering detected.
            </span>
          </div>
        ) : (
          <div className="flex items-start gap-2 border-b border-rose-400/30 bg-rose-950/40 px-7 py-2.5 text-xs text-rose-100">
            <ShieldAlert size={14} className="mt-0.5 shrink-0" />
            <span>
              Audit chain BROKEN
              {verify.data.first_broken_seq != null
                ? ` at seq ${verify.data.first_broken_seq}`
                : ""}
              {verify.data.reason ? ` — ${verify.data.reason}` : ""}. This trail
              may have been tampered with.
            </span>
          </div>
        )
      ) : null}
      {verify.error ? (
        <div className="border-b border-ink-800 px-7 py-2.5">
          <ErrorBanner error={verify.error} />
        </div>
      ) : null}
      {exportLog.error ? (
        <div className="border-b border-ink-800 px-7 py-2.5">
          <ErrorBanner error={exportLog.error} />
        </div>
      ) : null}

      <div className="relative z-10 min-h-0 flex-1 overflow-y-auto p-7">
        {isLoading ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : error ? (
          <ErrorBanner error={error} />
        ) : entries.length === 0 ? (
          <EmptyState
            title="No audit entries"
            description={
              filter
                ? `No actions match the prefix "${filter}".`
                : "Privileged actions in your tenant will appear here as they happen."
            }
          />
        ) : (
          <>
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Time</th>
                  <th className="px-3 py-2 font-medium">Actor</th>
                  <th className="px-3 py-2 font-medium">Action</th>
                  <th className="px-3 py-2 font-medium">Target</th>
                  <th className="px-3 py-2 font-medium">Details</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.id} className="align-top">
                    <td className="px-3 py-2.5 whitespace-nowrap text-ink-400">
                      {formatISTDateTime(entry.at)}
                    </td>
                    <td className="px-3 py-2.5 font-mono text-xs text-ink-300">
                      {entry.actor_id ? entry.actor_id.slice(0, 8) + "…" : "system"}
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="badge ring-accent-500/40 text-accent-300">
                        {entry.action}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-ink-200">
                      <span className="text-ink-400">{entry.target_kind}</span>{" "}
                      <span className="font-mono text-xs text-ink-200">
                        {entry.target_id ? entry.target_id.slice(0, 8) + "…" : "—"}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      {Object.keys(entry.payload).length > 0 ? (
                        <pre className="max-w-md overflow-x-auto rounded border border-ink-700 bg-ink-950/70 px-2 py-1.5 font-mono text-[11px] leading-5 text-ink-300">
                          {JSON.stringify(entry.payload, null, 2)}
                        </pre>
                      ) : (
                        <span className="text-ink-600">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="mt-5 flex items-center justify-between gap-3 text-xs text-ink-400">
              <span>
                Showing {pageStart}–{pageEnd} of {total}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
                  disabled={!canPrev || isFetching}
                >
                  <ChevronLeft size={14} />
                  Previous
                </button>
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setOffset((o) => o + PAGE_SIZE)}
                  disabled={!canNext || isFetching}
                >
                  Next
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
