import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Activity,
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

export function Layout() {
  const { claims, logout } = useAuth();
  const navigate = useNavigate();

  if (!claims) return null;

  const items = NAV.filter((n) => n.visibleTo.includes(claims.role));

  return (
    <div className="noise-shell flex h-full overflow-hidden bg-ink-950 text-ink-50">
      <aside className="flex w-64 shrink-0 flex-col border-r border-ink-700/80 bg-ink-950/78 shadow-[16px_0_60px_rgb(0_0_0/0.22)] backdrop-blur-xl">
        <div className="px-4 pb-5 pt-5">
          <Link to="/" className="group flex items-center gap-3">
            <span
              className="grid h-11 w-11 place-items-center rounded-md border border-accent-300 bg-accent-300 text-ink-950 shadow-[5px_5px_0_rgb(255_59_147/0.9)] transition group-hover:-rotate-2"
              aria-hidden="true"
            >
              <MorphLogo />
            </span>
            <span>
              <span className="block text-lg font-black uppercase tracking-[0.18em] text-ink-50">
                aakar
              </span>
              <span className="mt-0.5 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.22em] text-accent-200">
                <Sparkles size={11} />
                flow engine
              </span>
            </span>
          </Link>
        </div>
        <nav className="flex-1 space-y-1 px-3">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                [
                  "group flex items-center gap-3 rounded-md border px-3 py-2.5 text-sm font-semibold transition",
                  isActive
                    ? "border-accent-300/70 bg-accent-300/12 text-accent-100 shadow-[4px_4px_0_rgb(22_217_255/0.22)]"
                    : "border-transparent text-ink-300 hover:border-ink-700 hover:bg-ink-900/75 hover:text-ink-50",
                ].join(" ")
              }
            >
              <item.icon size={17} className="shrink-0 text-ink-400 transition group-hover:text-accent-200" />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-ink-700/80 px-3 py-4">
          <div className="mb-3 rounded-md border border-ink-700 bg-ink-900/70 px-3 py-2.5">
            <div className="truncate text-xs font-semibold text-ink-100">{claims.email}</div>
            <div className="mt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-signal-cyan">
              {roleLabel(claims)}
            </div>
          </div>
          <button
            type="button"
            className="btn-ghost w-full justify-start"
            onClick={() => {
              logout();
              navigate("/login");
            }}
          >
            <LogOut size={15} />
            Log out
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
