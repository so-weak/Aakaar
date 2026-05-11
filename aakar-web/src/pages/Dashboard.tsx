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
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { stats as statsApi, superuser as superuserApi } from "@/api";
import type {
  CapabilityUsage,
  DailyVolume,
  DashboardStats,
  FailureSummary,
  TenantVolume,
  VolumeBucket,
} from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { ErrorBanner } from "@/components/ErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { formatISTDateTime } from "@/lib/datetime";
import { useChartPalette } from "@/theme/ThemeProvider";

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
          <div className="space-y-6">
            <KpiStrip data={dashQ.data} canSeeLive={isAdmin || isSuper} />
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
              <div className="lg:col-span-2">
                <TrendChart data={dashQ.data.daily_volume} />
              </div>
              <StatusDonut bucket={dashQ.data.volume_24h} />
            </div>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
              <div className="lg:col-span-2">
                <CapabilityChart data={dashQ.data.capability_usage} />
              </div>
              <RecentFailuresPanel rows={dashQ.data.recent_failures} />
            </div>
            {dashQ.data.per_tenant ? (
              <PerTenantChart rows={dashQ.data.per_tenant} />
            ) : null}
          </div>
        ) : !dashQ.error ? (
          <DashboardSkeleton />
        ) : null}
      </div>
    </div>
  );
}

// ---------- KPI strip (cards with sparklines) ----------------------------

function KpiStrip({
  data,
  canSeeLive,
}: {
  data: DashboardStats;
  canSeeLive: boolean;
}) {
  const COLORS = useChartPalette();
  const totalRate = (b: VolumeBucket) => {
    const term = b.succeeded + b.failed;
    return term > 0 ? b.succeeded / term : null;
  };
  const total = (b: VolumeBucket) =>
    b.queued + b.running + b.paused + b.succeeded + b.failed + b.cancelled;

  // 24h sparkline: tail of the daily series (last 7 buckets is more useful
  // than 1 hourly bucket — gives a sense of where today fits in the week).
  const last7 = data.daily_volume.slice(-7);

  return (
    <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <KpiCard
        accent={COLORS.accent}
        label="Last 24 hours"
        value={total(data.volume_24h)}
        secondary={
          totalRate(data.volume_24h) === null
            ? "no terminal runs"
            : `${(totalRate(data.volume_24h)! * 100).toFixed(1)}% success`
        }
        sparkline={last7}
        sparklineKey="succeeded"
      />
      <KpiCard
        accent={COLORS.succeeded}
        label="Succeeded · 7d"
        value={data.volume_7d.succeeded}
        secondary={`${data.volume_7d.failed} failed`}
        sparkline={data.daily_volume.slice(-14)}
        sparklineKey="succeeded"
      />
      <KpiCard
        accent={COLORS.failed}
        label="Failed · 7d"
        value={data.volume_7d.failed}
        secondary={
          data.volume_7d.failed === 0 ? "clean week" : "needs attention"
        }
        sparkline={data.daily_volume.slice(-14)}
        sparklineKey="failed"
      />
      <ActiveKpi count={data.active_count} canSeeLive={canSeeLive} />
    </section>
  );
}

function KpiCard({
  accent,
  label,
  value,
  secondary,
  sparkline,
  sparklineKey,
}: {
  accent: string;
  label: string;
  value: number;
  secondary: string;
  sparkline: DailyVolume[];
  sparklineKey: "succeeded" | "failed";
}) {
  return (
    <div className="card relative overflow-hidden p-5">
      <div className="text-[11px] uppercase tracking-[0.18em] text-ink-500">
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <div className="text-3xl font-black text-ink-50 tabular-nums">{value}</div>
      </div>
      <div className="mt-1 text-xs text-ink-400">{secondary}</div>
      <div className="-mx-5 -mb-5 mt-3 h-12">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={sparkline} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient
                id={`spark-${sparklineKey}-${accent.slice(1)}`}
                x1="0"
                y1="0"
                x2="0"
                y2="1"
              >
                <stop offset="0%" stopColor={accent} stopOpacity={0.55} />
                <stop offset="100%" stopColor={accent} stopOpacity={0} />
              </linearGradient>
            </defs>
            <Area
              type="monotone"
              dataKey={sparklineKey}
              stroke={accent}
              strokeWidth={1.6}
              fill={`url(#spark-${sparklineKey}-${accent.slice(1)})`}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function ActiveKpi({
  count,
  canSeeLive,
}: {
  count: number;
  canSeeLive: boolean;
}) {
  return (
    <div className="card relative overflow-hidden p-5">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-[0.18em] text-ink-500">
        <Hourglass size={11} /> Active right now
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <div className="brand-glow-cyan text-3xl font-black tabular-nums text-signal-cyan">
          {count}
        </div>
      </div>
      <div className="mt-1 text-xs text-ink-400">queued / running / paused</div>
      {canSeeLive ? (
        <Link
          to="/live"
          className="btn-ghost mt-3 inline-flex items-center gap-1.5"
        >
          <Activity size={12} className="animate-pulse" />
          Live console
        </Link>
      ) : null}
    </div>
  );
}

// ---------- 30-day trend chart ------------------------------------------

function TrendChart({ data }: { data: DailyVolume[] }) {
  const COLORS = useChartPalette();
  const formatted = useMemo(
    () =>
      data.map((d) => ({
        ...d,
        label: formatShortIstDate(d.date),
      })),
    [data],
  );

  const empty = data.every(
    (d) =>
      d.succeeded + d.failed + d.paused + d.running + d.queued + d.cancelled === 0,
  );

  return (
    <div className="card p-5">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h3 className="flex items-center gap-2 text-base font-semibold text-ink-50">
            <TrendingUp size={14} className="text-accent-300" /> Run volume · last
            30 days
          </h3>
          <p className="mt-0.5 text-xs text-ink-500">
            Daily IST buckets · stacked by terminal status
          </p>
        </div>
      </div>
      <div className="h-[260px]">
        {empty ? (
          <EmptyChart message="No runs in the last 30 days yet." />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={formatted}
              margin={{ top: 12, right: 8, bottom: 4, left: -10 }}
            >
              <defs>
                <linearGradient id="trend-succeeded" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={COLORS.succeeded} stopOpacity={0.55} />
                  <stop offset="100%" stopColor={COLORS.succeeded} stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="trend-failed" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={COLORS.failed} stopOpacity={0.55} />
                  <stop offset="100%" stopColor={COLORS.failed} stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="trend-paused" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={COLORS.paused} stopOpacity={0.55} />
                  <stop offset="100%" stopColor={COLORS.paused} stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <XAxis
                dataKey="label"
                tick={{ fill: COLORS.axisText, fontSize: 10 }}
                axisLine={{ stroke: COLORS.axis }}
                tickLine={false}
                interval="preserveStartEnd"
                minTickGap={20}
              />
              <YAxis
                tick={{ fill: COLORS.axisText, fontSize: 10 }}
                axisLine={false}
                tickLine={false}
                allowDecimals={false}
                width={32}
              />
              <Tooltip content={<TrendTooltip />} />
              <Legend
                verticalAlign="top"
                height={28}
                iconType="circle"
                iconSize={8}
                wrapperStyle={{ fontSize: 11, color: COLORS.axisText }}
              />
              <Area
                type="monotone"
                stackId="v"
                dataKey="succeeded"
                stroke={COLORS.succeeded}
                strokeWidth={1.6}
                fill="url(#trend-succeeded)"
                isAnimationActive={false}
              />
              <Area
                type="monotone"
                stackId="v"
                dataKey="failed"
                stroke={COLORS.failed}
                strokeWidth={1.6}
                fill="url(#trend-failed)"
                isAnimationActive={false}
              />
              <Area
                type="monotone"
                stackId="v"
                dataKey="paused"
                stroke={COLORS.paused}
                strokeWidth={1.4}
                fill="url(#trend-paused)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}

interface TrendPoint {
  label: string;
  date: string;
  succeeded: number;
  failed: number;
  paused: number;
}

function TrendTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: TrendPoint }>;
}) {
  const COLORS = useChartPalette();
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-control border border-ink-700 bg-ink-950/95 px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-mono text-[11px] text-ink-300">{p.label}</div>
      <Stat color={COLORS.succeeded} label="succeeded" value={p.succeeded} />
      <Stat color={COLORS.failed} label="failed" value={p.failed} />
      <Stat color={COLORS.paused} label="paused" value={p.paused} />
    </div>
  );
}

function Stat({
  color,
  label,
  value,
}: {
  color: string;
  label: string;
  value: number;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="flex items-center gap-1.5 text-ink-400">
        <span
          className="h-2 w-2 rounded-full"
          style={{ background: color }}
        />
        {label}
      </span>
      <span className="font-mono tabular-nums text-ink-100">{value}</span>
    </div>
  );
}

// ---------- 24h status donut --------------------------------------------

function StatusDonut({ bucket }: { bucket: VolumeBucket }) {
  const COLORS = useChartPalette();
  const segments = [
    { name: "succeeded", value: bucket.succeeded, color: COLORS.succeeded },
    { name: "failed", value: bucket.failed, color: COLORS.failed },
    { name: "paused", value: bucket.paused, color: COLORS.paused },
    { name: "running", value: bucket.running, color: COLORS.running },
    { name: "queued", value: bucket.queued, color: COLORS.queued },
    { name: "cancelled", value: bucket.cancelled, color: COLORS.cancelled },
  ].filter((s) => s.value > 0);

  const total = segments.reduce((acc, s) => acc + s.value, 0);
  const terminal = bucket.succeeded + bucket.failed;
  const successRate = terminal > 0 ? bucket.succeeded / terminal : null;

  return (
    <div className="card flex flex-col p-5">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-semibold text-ink-50">Status · 24h</h3>
        <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-500">
          {total} runs
        </span>
      </div>
      <div className="relative flex flex-1 items-center justify-center">
        {total === 0 ? (
          <EmptyChart message="No activity in the last 24 hours." />
        ) : (
          <>
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie
                  data={segments}
                  innerRadius={56}
                  outerRadius={88}
                  paddingAngle={2}
                  dataKey="value"
                  stroke="transparent"
                  strokeWidth={2}
                  isAnimationActive={false}
                >
                  {segments.map((s) => (
                    <Cell key={s.name} fill={s.color} />
                  ))}
                </Pie>
                <Tooltip content={<DonutTooltip total={total} />} />
              </PieChart>
            </ResponsiveContainer>
            <div className="pointer-events-none absolute inset-0 grid place-items-center">
              <div className="text-center">
                <div className="text-2xl font-black tabular-nums text-ink-50">
                  {successRate === null
                    ? "—"
                    : `${(successRate * 100).toFixed(0)}%`}
                </div>
                <div className="text-[10px] uppercase tracking-[0.2em] text-ink-500">
                  success rate
                </div>
              </div>
            </div>
          </>
        )}
      </div>
      {total > 0 ? (
        <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
          {segments.map((s) => (
            <div key={s.name} className="flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-ink-400">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: s.color }}
                />
                {s.name}
              </span>
              <span className="font-mono tabular-nums text-ink-100">{s.value}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function DonutTooltip({
  active,
  payload,
  total,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number; payload: { color: string } }>;
  total: number;
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0];
  const pct = total > 0 ? (p.value / total) * 100 : 0;
  return (
    <div className="rounded-control border border-ink-700 bg-ink-950/95 px-3 py-2 text-xs shadow-lg">
      <div className="flex items-center gap-1.5 font-mono text-[11px] text-ink-200">
        <span
          className="h-2 w-2 rounded-full"
          style={{ background: p.payload.color }}
        />
        {p.name}
      </div>
      <div className="mt-1 font-mono tabular-nums text-ink-100">
        {p.value} <span className="text-ink-500">({pct.toFixed(1)}%)</span>
      </div>
    </div>
  );
}

// ---------- capability usage chart --------------------------------------

function CapabilityChart({ data }: { data: CapabilityUsage[] }) {
  const COLORS = useChartPalette();
  const rows = useMemo(
    () =>
      data.slice(0, 8).map((c) => ({
        ...c,
        succeeded: c.count - c.failure_count,
        ref_short: shortRef(c.capability_ref),
      })),
    [data],
  );

  return (
    <div className="card p-5">
      <h3 className="text-base font-semibold text-ink-50">
        Capability usage · last 7 days
      </h3>
      <p className="mt-0.5 text-xs text-ink-500">
        Top {rows.length || 8} by total invocations · failures highlighted
      </p>
      <div className="mt-4 h-[260px]">
        {rows.length === 0 ? (
          <EmptyChart message="No capability invocations in the last 7 days." />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={rows}
              layout="vertical"
              margin={{ top: 4, right: 16, bottom: 4, left: 8 }}
              barCategoryGap={10}
            >
              <XAxis
                type="number"
                tick={{ fill: COLORS.axisText, fontSize: 10 }}
                axisLine={false}
                tickLine={false}
                allowDecimals={false}
              />
              <YAxis
                type="category"
                dataKey="ref_short"
                tick={{ fill: COLORS.axisText, fontSize: 10, fontFamily: "ui-monospace, monospace" }}
                axisLine={false}
                tickLine={false}
                width={140}
              />
              <Tooltip content={<CapabilityTooltip />} />
              <Bar
                dataKey="succeeded"
                stackId="usage"
                fill={COLORS.succeeded}
                radius={[0, 0, 0, 0]}
                isAnimationActive={false}
              />
              <Bar
                dataKey="failure_count"
                stackId="usage"
                fill={COLORS.failed}
                radius={[0, 4, 4, 0]}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}

interface CapPoint {
  capability_ref: string;
  count: number;
  failure_count: number;
  succeeded: number;
}

function CapabilityTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: CapPoint }>;
}) {
  const COLORS = useChartPalette();
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-control border border-ink-700 bg-ink-950/95 px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-mono text-[11px] text-ink-200">
        {p.capability_ref}
      </div>
      <Stat color={COLORS.succeeded} label="succeeded" value={p.succeeded} />
      <Stat color={COLORS.failed} label="failed" value={p.failure_count} />
      <div className="mt-1 border-t border-ink-700/70 pt-1 font-mono text-[11px] text-ink-100">
        total {p.count}
      </div>
    </div>
  );
}

// ---------- recent failures list ----------------------------------------

function RecentFailuresPanel({ rows }: { rows: FailureSummary[] }) {
  return (
    <div className="card p-5">
      <h3 className="flex items-center gap-2 text-base font-semibold text-ink-50">
        <AlertTriangle size={14} className="text-rose-300" /> Recent failures
      </h3>
      <p className="mt-0.5 text-xs text-ink-500">
        {rows.length === 0 ? "Last 10 failures" : `Showing ${rows.length} most recent`}
      </p>
      <div className="mt-3 space-y-2">
        {rows.length === 0 ? (
          <div className="flex items-center gap-2 rounded-md border border-emerald-400/20 bg-emerald-400/5 px-3 py-2 text-sm text-emerald-300">
            <CheckCircle2 size={14} /> No failures in scope. Nice.
          </div>
        ) : (
          rows.map((r) => (
            <Link
              key={r.run_id}
              to={`/runs/${r.run_id}`}
              className="block rounded-md border border-ink-700/70 bg-ink-950/40 p-2.5 transition hover:border-rose-300/40 hover:bg-rose-400/5"
            >
              <div className="flex items-center gap-2">
                {r.tenant_slug ? (
                  <span className="badge ring-signal-pink/30 text-signal-pink">
                    {r.tenant_slug}
                  </span>
                ) : null}
                <span className="truncate text-sm font-semibold text-ink-100">
                  {r.workflow_name}
                </span>
              </div>
              <div className="mt-1 truncate text-xs text-rose-300">
                {r.error_type}
                <span className="ml-1 text-ink-400">{r.error_message}</span>
              </div>
              <div className="mt-1 font-mono text-[10px] text-ink-500">
                {formatISTDateTime(r.started_at)}
              </div>
            </Link>
          ))
        )}
      </div>
    </div>
  );
}

// ---------- per-tenant chart (super only) -------------------------------

function PerTenantChart({ rows }: { rows: TenantVolume[] }) {
  const COLORS = useChartPalette();
  if (rows.length === 0) {
    return (
      <div className="card p-6 text-center text-sm text-ink-500">
        <Building2 size={20} className="mx-auto mb-2 text-ink-600" />
        No tenant activity in the last 24 hours.
      </div>
    );
  }
  return (
    <div className="card p-5">
      <h3 className="flex items-center gap-2 text-base font-semibold text-ink-50">
        <Building2 size={14} className="text-signal-pink" /> Per-tenant volume ·
        last 24 hours
      </h3>
      <p className="mt-0.5 text-xs text-ink-500">
        Stacked by terminal status · sorted by total
      </p>
      <div className="mt-4 h-[280px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            margin={{ top: 4, right: 8, bottom: 4, left: -10 }}
            barCategoryGap={18}
          >
            <XAxis
              dataKey="tenant_slug"
              tick={{ fill: COLORS.axisText, fontSize: 11, fontFamily: "ui-monospace, monospace" }}
              axisLine={{ stroke: COLORS.axis }}
              tickLine={false}
              interval={0}
            />
            <YAxis
              tick={{ fill: COLORS.axisText, fontSize: 10 }}
              axisLine={false}
              tickLine={false}
              allowDecimals={false}
              width={32}
            />
            <Tooltip
              cursor={{ fill: "rgba(244,237,215,0.04)" }}
              content={<TenantTooltip />}
            />
            <Bar
              dataKey="succeeded"
              stackId="t"
              fill={COLORS.succeeded}
              isAnimationActive={false}
            />
            <Bar
              dataKey="failed"
              stackId="t"
              fill={COLORS.failed}
              radius={[4, 4, 0, 0]}
              isAnimationActive={false}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function TenantTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: TenantVolume }>;
}) {
  const COLORS = useChartPalette();
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-control border border-ink-700 bg-ink-950/95 px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-mono text-[11px] text-ink-100">{p.tenant_name}</div>
      <Stat color={COLORS.succeeded} label="succeeded" value={p.succeeded} />
      <Stat color={COLORS.failed} label="failed" value={p.failed} />
      <div className="mt-1 border-t border-ink-700/70 pt-1 text-[11px]">
        <span className="text-ink-400">success rate · </span>
        <span className="font-mono tabular-nums text-ink-100">
          {p.success_rate === null ? "—" : `${(p.success_rate * 100).toFixed(1)}%`}
        </span>
      </div>
    </div>
  );
}

// ---------- helpers ------------------------------------------------------

function shortRef(ref: string): string {
  // "cap.web_login" -> "cap.web_login" (already short)
  // "actions.http.request" -> "http.request"
  if (ref.length <= 22) return ref;
  return ref.slice(0, 21) + "…";
}

function formatShortIstDate(iso: string): string {
  // Input is yyyy-mm-dd already in IST. Render as "May 8" — short enough
  // for the x-axis but unambiguous over a 30-day window.
  const [, mm, dd] = iso.split("-");
  const months = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];
  const m = months[Number(mm) - 1] ?? mm;
  return `${m} ${Number(dd)}`;
}

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="grid h-full place-items-center text-center">
      <div>
        <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.22em] text-ink-600">
          no data
        </div>
        <div className="text-sm text-ink-500">{message}</div>
      </div>
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="card h-[120px] animate-pulse" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="card h-[320px] animate-pulse lg:col-span-2" />
        <div className="card h-[320px] animate-pulse" />
      </div>
    </div>
  );
}
