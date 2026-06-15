import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Activity,
  ChevronsLeft,
  ChevronsRight,
  CircleDot,
  ClipboardCheck,
  Gavel,
  KeyRound,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  MonitorSmartphone,
  Network,
  ScrollText,
  Sparkles,
  ShieldCheck,
  Users,
  Workflow,
} from "lucide-react";

import { useAuth } from "@/auth/AuthContext";
import { MorphLogo } from "@/components/MorphLogo";
import { TourButton } from "@/components/GuidedTour";
import { APP_TOUR } from "@/components/appTour";
import { LanguageSwitcher } from "@/i18n/LanguageSwitcher";
import { useLabels } from "@/i18n/LanguageProvider";
import type { LabelMap } from "@/i18n/labels";
import { ThemeSwitcher } from "@/theme/ThemeSwitcher";

interface NavItem {
  to: string;
  labelKey?: keyof LabelMap;
  // Plain-English fallback for nav items that have no i18n label yet.
  label?: string;
  icon: typeof Activity;
  visibleTo: ("superuser" | "tenant_admin" | "tenant_user")[];
}

// Nav items reference label *keys* — the label string is resolved against
// the active language at render time so flipping the language switcher
// re-labels the sidebar instantly without a remount.
const NAV: NavItem[] = [
  { to: "/dashboard", labelKey: "darshana", icon: LayoutDashboard, visibleTo: ["superuser", "tenant_admin", "tenant_user"] },
  { to: "/chat", labelKey: "samvada", icon: MessageSquare, visibleTo: ["tenant_admin", "tenant_user"] },
  { to: "/workflows", labelKey: "sutras", icon: Workflow, visibleTo: ["tenant_admin", "tenant_user"] },
  { to: "/runs", labelKey: "yajnas", icon: Activity, visibleTo: ["tenant_admin", "tenant_user"] },
  { to: "/approvals", label: "Approvals", icon: ClipboardCheck, visibleTo: ["tenant_admin", "tenant_user"] },
  { to: "/live", labelKey: "pratyaksha", icon: Activity, visibleTo: ["tenant_admin", "superuser"] },
  { to: "/capabilities", labelKey: "vidyas", icon: Network, visibleTo: ["superuser", "tenant_admin", "tenant_user"] },
  { to: "/mfa-settings", label: "Two-factor", icon: ShieldCheck, visibleTo: ["superuser", "tenant_admin", "tenant_user"] },
  { to: "/admin/users", labelKey: "sadhakas", icon: Users, visibleTo: ["tenant_admin"] },
  { to: "/admin/grants", labelKey: "kosha", icon: KeyRound, visibleTo: ["tenant_admin"] },
  { to: "/agents", label: "Agents", icon: MonitorSmartphone, visibleTo: ["tenant_admin"] },
  { to: "/recordings", label: "Recordings", icon: CircleDot, visibleTo: ["tenant_admin"] },
  { to: "/audit", label: "Audit log", icon: ScrollText, visibleTo: ["tenant_admin"] },
  { to: "/retention", label: "Retention", icon: Gavel, visibleTo: ["tenant_admin"] },
  { to: "/superuser/tenants", labelKey: "mandalas", icon: ShieldCheck, visibleTo: ["superuser"] },
  { to: "/superuser/users", labelKey: "sadhakas", icon: Users, visibleTo: ["superuser"] },
];

const COLLAPSED_KEY = "aakaar.sidebar.collapsed";

export function Layout() {
  const { claims, logout } = useAuth();
  const navigate = useNavigate();
  const labels = useLabels();

  // Persisted in sessionStorage so each tab keeps its own preference,
  // matching how we store the auth session.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    return sessionStorage.getItem(COLLAPSED_KEY) === "1";
  });
  useEffect(() => {
    sessionStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  if (!claims) return null;

  const items = NAV.filter((n) => n.visibleTo.includes(claims.role));

  return (
    <div className="noise-shell app-shell flex h-full overflow-hidden">
      <aside
        className={[
          // `relative z-20` establishes a stacking context above <main>
          // so the ThemeSwitcher popup can overflow the sidebar into the
          // main area without being covered by recharts SVGs.
          "app-sidebar relative z-20 flex shrink-0 flex-col transition-[width] duration-200",
          collapsed ? "w-16" : "w-64",
        ].join(" ")}
      >
        <div className={collapsed ? "px-2 pb-3 pt-5" : "px-4 pb-5 pt-5"}>
          <Link
            to="/"
            className={[
              "group flex items-center gap-3",
              collapsed ? "justify-center" : "",
            ].join(" ")}
            title={collapsed ? "AAKAAR" : undefined}
          >
            <span
              className="logo-tile grid h-11 w-11 shrink-0 place-items-center rounded-control transition group-hover:-rotate-2"
              aria-hidden="true"
            >
              <MorphLogo />
            </span>
            {collapsed ? null : (
              <span>
                <span className="headline block text-lg text-ink-50">AAKAAR</span>
                <span className="mt-0.5 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.22em] text-accent-200">
                  <Sparkles size={11} />
                  flow engine
                </span>
              </span>
            )}
          </Link>
        </div>

        <nav
          data-tour="nav"
          className={
            collapsed
              ? "min-h-0 flex-1 space-y-1 overflow-y-auto px-2"
              : "min-h-0 flex-1 space-y-1 overflow-y-auto px-3"
          }
        >
          {items.map((item) => {
            const text = item.label ?? (item.labelKey ? labels[item.labelKey] : item.to);
            // Stable hook for the guided tour to target individual nav links.
            const tourId = item.to.replace(/^\/+/, "").replace(/\//g, "-") || "home";
            return (
              <NavLink
                key={item.to}
                to={item.to}
                data-tour={`nav-${tourId}`}
                title={collapsed ? text : undefined}
                className={({ isActive }) =>
                  [
                    "nav-item group",
                    collapsed ? "justify-center px-0 py-2.5" : "px-3 py-2.5",
                    isActive ? "is-active" : "",
                  ].join(" ")
                }
              >
                <item.icon
                  size={17}
                  className="nav-icon shrink-0 transition group-hover:text-accent-200"
                />
                {collapsed ? null : <span>{text}</span>}
              </NavLink>
            );
          })}
        </nav>

        <div
          className={[
            "shrink-0 border-t border-ink-700/60",
            collapsed ? "px-2 py-3" : "px-3 py-4",
          ].join(" ")}
        >
          {collapsed ? (
            <div
              className="mb-2 grid h-9 w-full place-items-center rounded-control border border-ink-700 bg-ink-900/70 font-mono text-[11px] font-bold uppercase text-signal-cyan"
              title={`${claims.email} · ${roleLabel(claims, labels)}`}
            >
              {initials(claims.email)}
            </div>
          ) : (
            <div className="mb-3 rounded-control border border-ink-700/60 bg-ink-900/70 px-3 py-2.5">
              <div className="truncate text-xs font-semibold text-ink-100">
                {claims.email}
              </div>
              <div className="mt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-signal-cyan">
                {roleLabel(claims, labels)}
              </div>
            </div>
          )}

          <div className="mb-1.5">
            <LanguageSwitcher collapsed={collapsed} />
          </div>

          <div className="mb-1.5" data-tour="theme">
            <ThemeSwitcher collapsed={collapsed} />
          </div>

          <div className="mb-1.5">
            <TourButton
              steps={APP_TOUR}
              label={collapsed ? "" : "Take a tour"}
              className={collapsed ? "w-full" : "w-full justify-start"}
            />
          </div>

          <button
            type="button"
            className={[
              "btn-ghost w-full",
              collapsed ? "justify-center" : "justify-start",
            ].join(" ")}
            onClick={() => {
              logout();
              navigate("/login");
            }}
            title={collapsed ? labels.nirgama : undefined}
          >
            <LogOut size={15} />
            {collapsed ? null : labels.nirgama}
          </button>
          <button
            type="button"
            className={[
              "btn-ghost mt-1.5 w-full",
              collapsed ? "justify-center" : "justify-start",
            ].join(" ")}
            onClick={() => setCollapsed((c) => !c)}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? (
              <ChevronsRight size={15} />
            ) : (
              <>
                <ChevronsLeft size={15} />
                Collapse
              </>
            )}
          </button>
        </div>
      </aside>

      <main className="relative flex flex-1 flex-col overflow-hidden">
        <div className="pointer-events-none absolute right-8 top-6 z-0 hidden font-mono text-[10px] uppercase tracking-[0.35em] text-ink-700 lg:block">
          sankalpa / yantra / kriya
        </div>
        <Outlet />
      </main>
    </div>
  );
}

/**
 * Compose the role label shown in the sidebar.
 *  - superuser              → "Pracharya"
 *  - tenant_admin in PayOps → "PayOps Acharya"
 *  - tenant_user in PayOps  → "PayOps Sadhaka"
 *
 * Falls back to the raw role if no tenant label is known (e.g. a token
 * minted before the backend started returning tenant info).
 */
function roleLabel(
  claims: {
    role: "superuser" | "tenant_admin" | "tenant_user";
    tenant_name?: string | null;
    tenant_slug?: string | null;
  },
  labels: LabelMap,
): string {
  if (claims.role === "superuser") return labels.pracharya;
  const label = claims.tenant_name || claims.tenant_slug;
  const suffix = claims.role === "tenant_admin" ? labels.acharya : labels.sadhaka;
  if (!label) return suffix;
  return `${label} ${suffix}`;
}

function initials(email: string): string {
  // "soubhik.ghosh@payops.test" → "SG"
  // "admin@acme.test"          → "AD"
  const local = email.split("@")[0] || email;
  const parts = local.split(/[._-]/).filter(Boolean);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return local.slice(0, 2).toUpperCase();
}
