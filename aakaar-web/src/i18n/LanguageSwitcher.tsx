import { useEffect, useRef, useState } from "react";
import { Check, Languages } from "lucide-react";

import { LANGUAGES } from "@/i18n/labels";
import { useLang, useSetLang } from "@/i18n/LanguageProvider";

/**
 * Language switcher — sibling of ThemeSwitcher.
 *
 * Sits in the sidebar footer next to the theme switcher and the logout
 * button. Lets the operator toggle the noun vocabulary across English and
 * five Indic scripts. The active choice is persisted in localStorage.
 */

export function LanguageSwitcher({ collapsed }: { collapsed: boolean }) {
  const lang = useLang();
  const setLang = useSetLang();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape — matches ThemeSwitcher behavior.
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

  const active = LANGUAGES.find((l) => l.code === lang) ?? LANGUAGES[0];

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
        title={collapsed ? `Language · ${active.label}` : undefined}
      >
        <Languages size={15} />
        {collapsed ? null : (
          <span className="flex flex-1 items-center justify-between gap-2">
            <span className="truncate">Language</span>
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-400">
              {active.chip}
            </span>
          </span>
        )}
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Language picker"
          className="absolute bottom-full left-0 z-50 mb-2 w-60 overflow-hidden rounded-card border border-ink-700/70 bg-ink-950/95 shadow-[0_24px_60px_rgb(0_0_0/0.35)] backdrop-blur-xl"
        >
          <div className="border-b border-ink-700/60 px-3 py-2.5">
            <div className="panel-title">language</div>
            <div className="mt-0.5 text-xs text-ink-300">
              Labels only. Body text stays English.
            </div>
          </div>
          <div className="max-h-[60vh] overflow-y-auto px-1.5 py-1.5">
            {LANGUAGES.map((item) => {
              const isActive = item.code === lang;
              return (
                <button
                  key={item.code}
                  type="button"
                  role="menuitemradio"
                  aria-checked={isActive}
                  className={[
                    "flex w-full items-center gap-3 rounded-control px-2 py-2 text-left transition",
                    isActive
                      ? "bg-accent-300/12 text-ink-50"
                      : "text-ink-200 hover:bg-ink-800/60 hover:text-ink-50",
                  ].join(" ")}
                  onClick={() => {
                    setLang(item.code);
                    setOpen(false);
                  }}
                >
                  <span
                    aria-hidden="true"
                    className="grid h-8 w-8 shrink-0 place-items-center rounded-control border border-ink-700/60 bg-ink-900/60 font-mono text-[12px]"
                  >
                    {item.chip}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center justify-between gap-2">
                      <span className="text-sm font-semibold">
                        {item.nativeLabel}
                      </span>
                      {isActive ? (
                        <Check size={13} className="text-accent-300" />
                      ) : null}
                    </span>
                    <span className="block text-[11px] leading-snug text-ink-400">
                      {item.code === item.nativeLabel ? item.label : item.label}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
