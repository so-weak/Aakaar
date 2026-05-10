import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Activity,
  ChevronsLeft,
  ChevronsRight,
  KeyRound,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  Network,
  Sparkles,
  ShieldCheck,
  Users,
  Workflow,
} from "lucide-react";

import { useAuth } from "@/auth/AuthContext";
import { MorphLogo } from "@/components/MorphLogo";

interface NavItem {
  to: string;
  label: string;
  icon: typeof Activity;
  visibleTo: ("superuser" | "tenant_admin" | "tenant_user")[];
}

const NAV: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard, visibleTo: ["superuser", "tenant_admin", "tenant_user"] },
  { to: "/chat", label: "Chat", icon: MessageSquare, visibleTo: ["tenant_admin", "tenant_user"] },
  { to: "/workflows", label: "Workflows", icon: Workflow, visibleTo: ["tenant_admin", "tenant_user"] },
  { to: "/runs", label: "Runs", icon: Activity, visibleTo: ["tenant_admin", "tenant_user"] },
  { to: "/live", label: "Live", icon: Activity, visibleTo: ["tenant_admin", "superuser"] },
  { to: "/capabilities", label: "Capabilities", icon: Network, visibleTo: ["superuser", "tenant_admin", "tenant_user"] },
  { to: "/admin/users", label: "Users", icon: Users, visibleTo: ["tenant_admin"] },
  { to: "/admin/grants", label: "Grants", icon: KeyRound, visibleTo: ["tenant_admin"] },
  { to: "/superuser/tenants", label: "Tenants", icon: ShieldCheck, visibleTo: ["superuser"] },
  { to: "/superuser/users", label: "All users", icon: Users, visibleTo: ["superuser"] },
];

const COLLAPSED_KEY = "aakar.sidebar.collapsed";

export function Layout() {
  const { claims, logout } = useAuth();
  const navigate = useNavigate();

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
    <div className="noise-shell flex h-full overflow-hidden bg-ink-950 text-ink-50">
      <aside
        className={[
          "flex shrink-0 flex-col border-r border-ink-700/80 bg-ink-950/78 shadow-[16px_0_60px_rgb(0_0_0/0.22)] backdrop-blur-xl transition-[width] duration-200",
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
            title={collapsed ? "Aakar" : undefined}
          >
            <span
              className="grid h-11 w-11 shrink-0 place-items-center rounded-md border border-accent-300 bg-accent-300 text-ink-950 shadow-[5px_5px_0_rgb(255_59_147/0.9)] transition group-hover:-rotate-2"
              aria-hidden="true"
            >
              <MorphLogo />
            </span>
            {collapsed ? null : (
              <span>
                <span className="block text-lg font-black uppercase tracking-[0.18em] text-ink-50">
                  aakar
                </span>
                <span className="mt-0.5 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.22em] text-accent-200">
                  <Sparkles size={11} />
                  flow engine
                </span>
              </span>
            )}
          </Link>
        </div>

        <nav className={collapsed ? "flex-1 space-y-1 px-2" : "flex-1 space-y-1 px-3"}>
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              title={collapsed ? item.label : undefined}
              className={({ isActive }) =>
                [
                  "group flex items-center gap-3 rounded-md border text-sm font-semibold transition",
                  collapsed ? "justify-center px-0 py-2.5" : "px-3 py-2.5",
                  isActive
                    ? "border-accent-300/70 bg-accent-300/12 text-accent-100 shadow-[4px_4px_0_rgb(22_217_255/0.22)]"
                    : "border-transparent text-ink-300 hover:border-ink-700 hover:bg-ink-900/75 hover:text-ink-50",
                ].join(" ")
              }
            >
              <item.icon size={17} className="shrink-0 text-ink-400 transition group-hover:text-accent-200" />
              {collapsed ? null : <span>{item.label}</span>}
            </NavLink>
          ))}
        </nav>

        <div
          className={[
            "border-t border-ink-700/80",
            collapsed ? "px-2 py-3" : "px-3 py-4",
          ].join(" ")}
        >
          {collapsed ? (
            <div
              className="mb-2 grid h-9 w-full place-items-center rounded-md border border-ink-700 bg-ink-900/70 font-mono text-[11px] font-bold uppercase text-signal-cyan"
              title={`${claims.email} · ${roleLabel(claims)}`}
            >
              {initials(claims.email)}
            </div>
          ) : (
            <div className="mb-3 rounded-md border border-ink-700 bg-ink-900/70 px-3 py-2.5">
              <div className="truncate text-xs font-semibold text-ink-100">
                {claims.email}
              </div>
              <div className="mt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-signal-cyan">
                {roleLabel(claims)}
              </div>
            </div>
          )}
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
            title={collapsed ? "Log out" : undefined}
          >
            <LogOut size={15} />
            {collapsed ? null : "Log out"}
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
          natural language / dag / action
        </div>
        <Outlet />
      </main>
    </div>
  );
}

/**
 * Compose the role label shown in the sidebar.
 *  - superuser              → "superuser"
 *  - tenant_admin in PayOps → "PayOps admin"
 *  - tenant_user in PayOps  → "PayOps user"
 *
 * Falls back to the raw role if no tenant label is known (e.g. a token
 * minted before the backend started returning tenant info).
 */
function roleLabel(claims: {
  role: "superuser" | "tenant_admin" | "tenant_user";
  tenant_name?: string | null;
  tenant_slug?: string | null;
}): string {
  if (claims.role === "superuser") return "superuser";
  const label = claims.tenant_name || claims.tenant_slug;
  if (!label) return claims.role.replace("_", " ");
  const suffix = claims.role === "tenant_admin" ? "admin" : "user";
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
