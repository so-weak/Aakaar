import { useEffect, useRef, useState } from "react";
import { Check, Palette } from "lucide-react";

import { THEMES, useTheme } from "@/theme/ThemeProvider";
import type { ThemeFamily, ThemeMeta } from "@/theme/ThemeProvider";

/**
 * Theme switcher
 * --------------------------------------------------------------------------
 * A small popover triggered by a circular icon button in the sidebar. Lists
 * themes grouped by family so the modes within each (light + dark) sit
 * next to each other.
 *
 * Compact mode (`collapsed=true`) renders just the icon button — used when
 * the sidebar is collapsed.
 */

const FAMILY_ORDER: ThemeFamily[] = [
  "neon-grunge",
  "skeuomorphic",
  "retro-futurism",
  "minimalism",
  "hdfc",
];

function groupedThemes(): Array<{
  family: ThemeFamily;
  familyLabel: string;
  items: ThemeMeta[];
}> {
  return FAMILY_ORDER.map((family) => {
    const items = THEMES.filter((t) => t.family === family);
    return { family, familyLabel: items[0]?.familyLabel ?? family, items };
  });
}

export function ThemeSwitcher({ collapsed }: { collapsed: boolean }) {
  const { meta, setTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape so the popover behaves like a menu.
  useEffect(() => {
    if (!open) return;
    function onClick(event: MouseEvent) {
      if (!ref.current) return;
      if (!ref.current.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const groups = groupedThemes();

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        className={[
          "btn-ghost w-full",
          collapsed ? "justify-center" : "justify-start",
        ].join(" ")}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={collapsed ? `Theme · ${meta.label}` : undefined}
      >
        <Palette size={15} />
        {collapsed ? null : (
          <span className="flex flex-1 items-center justify-between gap-2">
            <span className="truncate">Theme</span>
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-400">
              {meta.familyLabel.split(" ")[0]} · {meta.mode}
            </span>
          </span>
        )}
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Theme picker"
          className="absolute bottom-full left-0 z-50 mb-2 w-72 overflow-hidden rounded-card border border-ink-700/70 bg-ink-950/95 shadow-[0_24px_60px_rgb(0_0_0/0.35)] backdrop-blur-xl"
        >
          <div className="border-b border-ink-700/60 px-3 py-2.5">
            <div className="panel-title">theme</div>
            <div className="mt-0.5 text-xs text-ink-300">
              Aesthetic + mode. Persists per device.
            </div>
          </div>
          <div className="max-h-[50vh] overflow-y-auto px-1.5 py-1.5">
            {groups.map((group) => (
              <div key={group.family} className="px-1.5 py-1">
                <div className="px-1.5 pb-1 font-mono text-[10px] uppercase tracking-[0.18em] text-ink-500">
                  {group.familyLabel}
                </div>
                <div className="space-y-0.5">
                  {group.items.map((item) => {
                    const isActive = item.id === meta.id;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        role="menuitemradio"
                        aria-checked={isActive}
                        className={[
                          "flex w-full items-start gap-2.5 rounded-control px-2 py-2 text-left transition",
                          isActive
                            ? "bg-accent-300/12 text-ink-50"
                            : "text-ink-200 hover:bg-ink-800/60 hover:text-ink-50",
                        ].join(" ")}
                        onClick={() => {
                          setTheme(item.id);
                          setOpen(false);
                        }}
                      >
                        <ThemePreviewSwatch themeId={item.id} />
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center justify-between gap-2">
                            <span className="text-sm font-semibold">
                              {item.mode === "light" ? "Light" : "Dark"}
                            </span>
                            {isActive ? (
                              <Check size={13} className="text-accent-300" />
                            ) : null}
                          </span>
                          <span className="block text-[11px] leading-snug text-ink-400">
                            {item.description}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Tiny three-stripe swatch that previews each theme's palette. Renders
 * inline-style with hardcoded HEX values rather than CSS variables because
 * the swatch needs to show *that* theme regardless of the active theme.
 */
function ThemePreviewSwatch({ themeId }: { themeId: ThemeMeta["id"] }) {
  const stripes = SWATCH[themeId];
  return (
    <span
      aria-hidden="true"
      className="mt-0.5 grid h-9 w-9 shrink-0 grid-cols-3 overflow-hidden rounded-control border border-ink-700/60"
      style={{ background: stripes[0] }}
    >
      <span style={{ background: stripes[0] }} />
      <span style={{ background: stripes[1] }} />
      <span style={{ background: stripes[2] }} />
    </span>
  );
}

const SWATCH: Record<ThemeMeta["id"], [string, string, string]> = {
  "neon-grunge-dark": ["#0b0b0a", "#d9fb1d", "#ff3b93"],
  "neon-grunge-light": ["#faf5e6", "#0b0b0a", "#d61f6e"],
  "skeuomorphic-light": ["#eaf1fb", "#0c91a7", "#e85674"],
  "skeuomorphic-dark": ["#1c1814", "#dc8a26", "#e0566e"],
  "retro-futurism-dark": ["#0e0a1e", "#ff41b8", "#5cebf4"],
  "retro-futurism-light": ["#fff5fb", "#e840b2", "#3cb4dc"],
  "minimalism-light": ["#ffffff", "#08090c", "#be421e"],
  "minimalism-dark": ["#0e0e0d", "#f8f8f7", "#dc6e3c"],
  "hdfc-light": ["#ffffff", "#004c8f", "#ed232a"],
  "hdfc-dark": ["#09162f", "#4fa3e0", "#ff3d44"],
};
