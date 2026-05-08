import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Building2,
  CheckCircle2,
  Hourglass,
  TrendingUp,
} from "lucide-react";

import { stats as statsApi, superuser as superuserApi } from "@/api";
import type {
  DashboardStats,
  FailureSummary,
  TenantVolume,
  VolumeBucket,
} from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDateTime } from "@/lib/datetime";

export function DashboardPage() {
  const { claims } = useAuth();
  const isSuper = claims?.role === "superuser";
  const isAdmin = claims?.role === "tenant_admin";

  const dashQ = useQuery<DashboardStats>({
    queryKey: ["dashboard", isSuper ? "global" : "scoped"],
    queryFn: () =>
      isSuper ? superuserApi.getDashboard() : statsApi.getDashboard(),
    refetchInterval: 15_000,
  });

  const subtitle = useMemo(() => {
    const scope = dashQ.data?.scope;
    if (scope === "global") return "Cross-tenant overview · refreshes every 15s";
    if (scope === "tenant") return "Tenant overview · refreshes every 15s";
    if (scope === "user") return "Your activity · refreshes every 15s";
    return "Loading…";
  }, [dashQ.data]);

  const title = isSuper
    ? "Operator console"
    : isAdmin
      ? "Tenant overview"
      : "Your activity";

  return (
    <div className="flex h-full flex-col">
      <PageHeader title={title} subtitle={subtitle} />

      <div className="relative z-10 flex-1 overflow-y-auto p-7">
        {dashQ.error ? <ErrorBanner error={dashQ.error} /> : null}

        {dashQ.data ? (
          <div className="space-y-7">
            <VolumeStrip data={dashQ.data} />
            <ActiveAndFailures data={dashQ.data} canSeeLive={isAdmin || isSuper} />
            <CapabilityUsageSection data={dashQ.data} />
            {dashQ.data.per_tenant ? (
              <PerTenantSection rows={dashQ.data.per_tenant} />
            ) : null}
          </div>
        ) : !dashQ.error ? (
          <div className="text-sm text-ink-400">Loading…</div>
        ) : null}
      </div>
    </div>
  );
}

// ---------- volume strip ------------------------------------------------

function VolumeStrip({ data }: { data: DashboardStats }) {
  return (
    <section>
      <h2 className="panel-title mb-3 flex items-center gap-2">
        <TrendingUp size={12} /> Run volume
      </h2>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <VolumeCard label="Last 24 hours" bucket={data.volume_24h} />
        <VolumeCard label="Last 7 days" bucket={data.volume_7d} />
        <VolumeCard label="Last 30 days" bucket={data.volume_30d} />
      </div>
    </section>
  );
}

function VolumeCard({ label, bucket }: { label: string; bucket: VolumeBucket }) {
  const total =
    bucket.queued +
    bucket.running +
    bucket.paused +
    bucket.succeeded +
    bucket.failed +
    bucket.cancelled;
  const terminal = bucket.succeeded + bucket.failed;
  const successRate = terminal > 0 ? bucket.succeeded / terminal : null;

  return (
    <div className="card p-5">
      <div className="text-xs uppercase tracking-wider text-ink-500">{label}</div>
      <div className="mt-1 flex items-baseline gap-3">
        <div className="text-3xl font-black text-ink-50">{total}</div>
        <div className="text-xs text-ink-400">runs</div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <Row label="succeeded" value={bucket.succeeded} color="text-emerald-300" />
        <Row label="failed" value={bucket.failed} color="text-rose-300" />
        <Row label="running" value={bucket.running} color="text-signal-cyan" />
        <Row label="paused" value={bucket.paused} color="text-amber-300" />
        <Row label="queued" value={bucket.queued} color="text-ink-300" />
        <Row label="cancelled" value={bucket.cancelled} color="text-ink-400" />
      </div>
      <div className="mt-3 border-t border-ink-700/70 pt-3">
        <div className="flex items-center justify-between text-xs">
          <span className="text-ink-400">Success rate</span>
          <span className="font-mono text-ink-100">
            {successRate === null
              ? "—"
              : `${(successRate * 100).toFixed(1)}%`}
          </span>
        </div>
        <SuccessBar rate={successRate} />
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  color,
}: {
  label: string;
  value: number;
  color: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-ink-400">{label}</span>
      <span className={`font-mono ${color}`}>{value}</span>
    </div>
  );
}

function SuccessBar({ rate }: { rate: number | null }) {
  if (rate === null) {
    return (
      <div className="mt-2 h-1.5 w-full rounded-full bg-ink-800/70" />
    );
  }
  const pct = Math.round(rate * 100);
  const color =
    rate >= 0.95
      ? "bg-emerald-400"
      : rate >= 0.8
        ? "bg-amber-400"
        : "bg-rose-400";
  return (
    <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-ink-800/70">
      <div
        className={`h-full ${color} transition-all`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

// ---------- active + recent failures -----------------------------------

function ActiveAndFailures({
  data,
  canSeeLive,
}: {
  data: DashboardStats;
  canSeeLive: boolean;
}) {
  return (
    <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <div className="card p-5 lg:col-span-1">
        <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-ink-500">
          <Hourglass size={12} /> Active right now
        </div>
        <div className="mt-2 flex items-baseline gap-3">
          <div className="text-3xl font-black text-signal-cyan">
            {data.active_count}
          </div>
          <div className="text-xs text-ink-400">queued / running / paused</div>
        </div>
        {canSeeLive ? (
          <Link
            to="/live"
            className="btn-ghost mt-4 inline-flex items-center gap-1.5"
          >
            <Activity size={12} /> Open live console
          </Link>
        ) : null}
      </div>

      <div className="card p-5 lg:col-span-2">
        <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-wider text-ink-500">
          <AlertTriangle size={12} /> Recent failures
        </div>
        {data.recent_failures.length === 0 ? (
          <div className="flex items-center gap-2 text-sm text-emerald-300">
            <CheckCircle2 size={14} /> No failures in scope. Nice.
          </div>
        ) : (
          <FailuresTable rows={data.recent_failures} />
        )}
      </div>
    </section>
  );
}

function FailuresTable({ rows }: { rows: FailureSummary[] }) {
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
        <tr>
          <th className="px-2 py-2 font-medium">Workflow</th>
          <th className="px-2 py-2 font-medium">Error</th>
          <th className="px-2 py-2 font-medium">When</th>
          <th className="px-2 py-2 font-medium" />
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.run_id} className="border-t border-ink-700/40">
            <td className="px-2 py-2.5">
              <div className="flex items-center gap-2">
                {r.tenant_slug ? (
                  <span className="badge ring-signal-pink/30 text-signal-pink">
                    {r.tenant_slug}
                  </span>
                ) : null}
                <span className="text-ink-100">{r.workflow_name}</span>
              </div>
            </td>
            <td className="px-2 py-2.5">
              <div className="text-rose-300">{r.error_type}</div>
              <div className="truncate text-xs text-ink-400" title={r.error_message}>
                {r.error_message}
              </div>
            </td>
            <td className="px-2 py-2.5 text-ink-400">
              {formatISTDateTime(r.started_at)}
            </td>
            <td className="px-2 py-2.5 text-right">
              <Link to={`/runs/${r.run_id}`} className="btn-ghost">
                Open
              </Link>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------- capability usage -------------------------------------------

function CapabilityUsageSection({ data }: { data: DashboardStats }) {
  if (data.capability_usage.length === 0) {
    return null;
  }
  const max = Math.max(...data.capability_usage.map((c) => c.count));
  return (
    <section className="card p-5">
      <h2 className="panel-title mb-3">Capability usage · last 7 days</h2>
      <div className="space-y-2">
        {data.capability_usage.map((c) => {
          const failRate = c.count > 0 ? c.failure_count / c.count : 0;
          const widthPct = Math.max(2, Math.round((c.count / max) * 100));
          return (
            <div
              key={c.capability_ref}
              className="grid grid-cols-12 items-center gap-3 text-sm"
            >
              <div className="col-span-4 truncate font-mono text-xs text-ink-100">
                {c.capability_ref}
              </div>
              <div className="col-span-6">
                <div className="h-2 w-full overflow-hidden rounded-full bg-ink-800/70">
                  <div
                    className="h-full bg-accent-300/80"
                    style={{ width: `${widthPct}%` }}
                  />
                </div>
              </div>
              <div className="col-span-2 flex items-center justify-end gap-2 font-mono text-xs">
                <span className="text-ink-200">{c.count}</span>
                {c.failure_count > 0 ? (
                  <span
                    className="text-rose-300"
                    title={`${c.failure_count} failures (${(failRate * 100).toFixed(0)}%)`}
                  >
                    ↯{c.failure_count}
                  </span>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------- per-tenant (super only) ------------------------------------

function PerTenantSection({ rows }: { rows: TenantVolume[] }) {
  return (
    <section>
      <h2 className="panel-title mb-3 flex items-center gap-2">
        <Building2 size={12} /> Per-tenant volume · last 24 hours
      </h2>
      <div className="card overflow-hidden p-0">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wider text-ink-500">
            <tr className="bg-ink-950/60">
              <th className="px-4 py-2.5 font-medium">Tenant</th>
              <th className="px-4 py-2.5 font-medium">Total</th>
              <th className="px-4 py-2.5 font-medium">Succeeded</th>
              <th className="px-4 py-2.5 font-medium">Failed</th>
              <th className="px-4 py-2.5 font-medium">Success rate</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-sm text-ink-500">
                  No tenant activity in the last 24 hours.
                </td>
              </tr>
            ) : (
              rows.map((r) => (
                <tr key={r.tenant_id} className="border-t border-ink-700/40">
                  <td className="px-4 py-2.5">
                    <Link
                      to={`/superuser/tenants/${r.tenant_id}`}
                      className="text-ink-100 hover:text-accent-200"
                    >
                      {r.tenant_name}
                    </Link>
                    <div className="font-mono text-[11px] text-ink-500">
                      {r.tenant_slug}
                    </div>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-ink-200">{r.total}</td>
                  <td className="px-4 py-2.5 font-mono text-emerald-300">
                    {r.succeeded}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-rose-300">
                    {r.failed}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-ink-100">
                    {r.success_rate === null
                      ? "—"
                      : `${(r.success_rate * 100).toFixed(1)}%`}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
