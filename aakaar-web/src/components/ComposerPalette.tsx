// ComposerPalette — keyboard-first quick-insert for the chat composer.
//
// Two modes, both surfaced as one accessible listbox above the textarea:
//   • slash commands — typing "/" at the start of a line offers /save, /run,
//     /refine, /clear-draft, /examples (the high-frequency plan actions).
//   • capability insert — the composer's "Advanced" toggle lists live-filtered,
//     kind-coloured capability refs to drop into the message at the caret.
//
// The textarea keeps focus (it is the ARIA combobox); this list is controlled
// by the parent via aria-activedescendant, so all navigation happens from the
// textarea's onKeyDown. Items use onMouseDown+preventDefault so a click never
// steals focus from the textarea. Presentational only — the parent owns open
// state, the active index, and what selection does.

import { Zap } from "lucide-react";
import type { CapabilityDefinitionResponse, NodeKind } from "@/api/types";
import { useChatStrings } from "@/i18n/chatStrings";

export type PaletteItemKind = "command" | "capability";

export interface PaletteItem {
  /** DOM id, referenced by the combobox's aria-activedescendant. */
  id: string;
  kind: PaletteItemKind;
  title: string;
  subtitle?: string;
  /** Capability node kind, for chip colour. */
  nodeKind?: NodeKind;
  /** Command name ("save") or capability ref ("cap.web_click"). */
  payload: string;
}

export interface SlashCommand {
  name: string;
  title: string;
  subtitle: string;
}

// The slash commands offered at line-start. Kept small, memorable, and each
// mapped to a real action the composer's parent can perform.
export const SLASH_COMMANDS: SlashCommand[] = [
  { name: "run", title: "/run", subtitle: "Save (if needed) and run this workflow" },
  { name: "save", title: "/save", subtitle: "Save the current plan as a workflow" },
  { name: "plan", title: "/plan", subtitle: "Open the plan view" },
];

const KIND_CHIP: Record<NodeKind, string> = {
  capability: "text-emerald-300 border-emerald-300/30 bg-emerald-300/5",
  action: "text-signal-cyan border-signal-cyan/30 bg-signal-cyan/5",
  control: "text-signal-pink border-signal-pink/30 bg-signal-pink/5",
};

const PALETTE_ID_PREFIX = "composer-palette-item-";

export function paletteItemDomId(index: number): string {
  return `${PALETTE_ID_PREFIX}${index}`;
}

/**
 * Build slash-command items matching the fragment typed after "/".
 * `subtitles` optionally overrides each command's subtitle with translated copy
 * (keyed by command name); falls back to the English constant.
 */
export function matchCommands(
  fragment: string,
  subtitles?: Record<string, string>,
): PaletteItem[] {
  const f = fragment.toLowerCase();
  return SLASH_COMMANDS.filter((c) => !f || c.name.startsWith(f)).map((c, i) => ({
    id: paletteItemDomId(i),
    kind: "command" as const,
    title: c.title,
    subtitle: subtitles?.[c.name] ?? c.subtitle,
    payload: c.name,
  }));
}

/** Build capability items matching a free-text filter. */
export function matchCapabilities(
  caps: CapabilityDefinitionResponse[],
  filter: string,
  limit = 8,
): PaletteItem[] {
  const f = filter.toLowerCase();
  return caps
    .filter(
      (c) =>
        !f ||
        c.ref.toLowerCase().includes(f) ||
        c.description.toLowerCase().includes(f) ||
        c.tags?.some((t) => t.toLowerCase().includes(f)),
    )
    .slice(0, limit)
    .map((c, i) => ({
      id: paletteItemDomId(i),
      kind: "capability" as const,
      title: c.ref.replace(/^cap\./, ""),
      subtitle: c.description,
      nodeKind: c.kind,
      payload: c.ref,
    }));
}

interface ComposerPaletteProps {
  items: PaletteItem[];
  activeIndex: number;
  mode: PaletteItemKind;
  listboxId: string;
  onSelect: (item: PaletteItem) => void;
  onHover: (index: number) => void;
}

export function ComposerPalette({
  items,
  activeIndex,
  mode,
  listboxId,
  onSelect,
  onHover,
}: ComposerPaletteProps) {
  const cs = useChatStrings();
  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-ink-800/80 bg-ink-950/90 shadow-lg backdrop-blur">
      <p className="flex items-center gap-1.5 border-b border-ink-800/70 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-ink-500">
        {mode === "command" ? (
          <>{cs.paletteCommands}</>
        ) : (
          <>
            <Zap size={10} className="text-accent-300" /> {cs.paletteInsertCap}
          </>
        )}
      </p>
      <ul id={listboxId} role="listbox" className="max-h-56 overflow-y-auto p-1">
        {items.length === 0 ? (
          <li className="px-3 py-2 text-[11px] text-ink-600">{cs.paletteNoMatches}</li>
        ) : (
          items.map((item, index) => {
            const active = index === activeIndex;
            return (
              <li
                key={item.id}
                id={item.id}
                role="option"
                aria-selected={active}
                onMouseDown={(e) => {
                  e.preventDefault();
                  onSelect(item);
                }}
                onMouseEnter={() => onHover(index)}
                className={[
                  "flex cursor-pointer items-center justify-between gap-3 rounded-lg px-2.5 py-1.5 text-left transition",
                  active ? "bg-accent-300/10 text-ink-50" : "text-ink-300",
                ].join(" ")}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {item.kind === "capability" && item.nodeKind ? (
                    <span
                      className={[
                        "shrink-0 rounded border px-1 py-0.5 font-mono text-[9px]",
                        KIND_CHIP[item.nodeKind],
                      ].join(" ")}
                    >
                      {item.nodeKind}
                    </span>
                  ) : null}
                  <span
                    className={[
                      "truncate",
                      item.kind === "command"
                        ? "font-mono text-xs"
                        : "font-mono text-[11px]",
                    ].join(" ")}
                  >
                    {item.title}
                  </span>
                </span>
                {item.subtitle ? (
                  <span className="hidden truncate text-[10px] text-ink-500 sm:block">
                    {item.subtitle}
                  </span>
                ) : null}
              </li>
            );
          })
        )}
      </ul>
    </div>
  );
}
