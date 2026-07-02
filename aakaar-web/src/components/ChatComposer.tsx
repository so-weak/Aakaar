// ChatComposer — the app's single message-input surface.
//
// One accessible, auto-growing composer used everywhere the operator talks to
// the planner. Highlights:
//   • auto-grows from one line to max-h-40, then scrolls;
//   • Enter sends, Shift+Enter is a newline, ⌘/Ctrl+Enter also sends (muscle
//     memory), all guarded by IME composition so candidate selection in
//     non-Latin scripts never fires a premature send;
//   • the primary button flips from Send to a real Stop while a turn is in
//     flight (the parent wires an AbortController);
//   • a visible keyboard hint (not a placeholder-only secret) and a proper
//     sr-only label + aria-describedby;
//   • an ARIA combobox driving ComposerPalette: "/" at line-start opens slash
//     commands, the Advanced (⚡) toggle opens capability quick-insert. Nav
//     happens from here via aria-activedescendant so the textarea keeps focus.

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, RefObject } from "react";
import { Send, StopCircle, Zap } from "lucide-react";

import type { CapabilityDefinitionResponse } from "@/api/types";
import {
  ComposerPalette,
  matchCapabilities,
  matchCommands,
  paletteItemDomId,
} from "@/components/ComposerPalette";
import type { PaletteItem } from "@/components/ComposerPalette";
import { useChatStrings } from "@/i18n/chatStrings";

interface ChatComposerProps {
  value: string;
  onChange: (v: string) => void;
  /** Send the current message. Parent validates + clears after the echo lands. */
  onSubmit: () => void;
  /** Abort the in-flight planner turn. */
  onStop: () => void;
  isPending: boolean;
  capabilities: CapabilityDefinitionResponse[];
  /** Dispatch a slash command (name without the leading "/"). */
  onCommand: (name: string) => void;
  placeholder?: string;
  hint?: string;
  /** Optional external ref so the parent can focus the textarea. */
  textareaRef?: RefObject<HTMLTextAreaElement>;
}

const SLASH_LINE = /^\/([a-z-]*)$/;

export function ChatComposer({
  value,
  onChange,
  onSubmit,
  onStop,
  isPending,
  capabilities,
  onCommand,
  placeholder,
  hint,
  textareaRef,
}: ChatComposerProps) {
  const cs = useChatStrings();
  const innerRef = useRef<HTMLTextAreaElement>(null);
  const ref = textareaRef ?? innerRef;
  const [showCaps, setShowCaps] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const listboxId = useId();
  const hintId = useId();
  const labelId = useId();

  // Auto-grow: reset to measure, then clamp to the CSS max-height.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [value, ref]);

  // Slash mode wins over capability mode. The fragment is whatever follows the
  // "/" on the current (last) line.
  const lastLine = value.slice(value.lastIndexOf("\n") + 1);
  const slashMatch = lastLine.match(SLASH_LINE);
  const slashOpen = !dismissed && slashMatch != null;
  const capsOpen = !slashOpen && showCaps;

  const mode: PaletteItem["kind"] = slashOpen ? "command" : "capability";
  const items = useMemo<PaletteItem[]>(() => {
    if (slashOpen)
      return matchCommands(slashMatch?.[1] ?? "", {
        run: cs.cmdRunSub,
        save: cs.cmdSaveSub,
        plan: cs.cmdPlanSub,
      });
    if (capsOpen) return matchCapabilities(capabilities, value);
    return [];
  }, [slashOpen, capsOpen, slashMatch, capabilities, value, cs]);

  const paletteOpen = (slashOpen || capsOpen) && items.length >= 0;
  const showPalette = paletteOpen && (items.length > 0 || capsOpen);

  // Keep the active row in range as the list changes.
  useEffect(() => {
    setActiveIndex((i) => (items.length === 0 ? 0 : Math.min(i, items.length - 1)));
  }, [items.length]);

  const focusTextarea = () => requestAnimationFrame(() => ref.current?.focus());

  const selectItem = (item: PaletteItem) => {
    if (item.kind === "command") {
      // Drop the "/cmd" line the user was typing, then dispatch.
      const cut = value.lastIndexOf("\n");
      onChange(cut >= 0 ? value.slice(0, cut + 1) : "");
      setDismissed(true);
      onCommand(item.payload);
    } else {
      const next = value.trim() ? `${value.trim()} ${item.payload}` : item.payload;
      onChange(next);
      setShowCaps(false);
    }
    focusTextarea();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const composing = e.nativeEvent.isComposing;

    if (showPalette && items.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) => (i + 1) % items.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) => (i - 1 + items.length) % items.length);
        return;
      }
      if ((e.key === "Enter" || e.key === "Tab") && !composing) {
        e.preventDefault();
        selectItem(items[activeIndex]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowCaps(false);
        setDismissed(true);
        return;
      }
    }

    // Submit shortcuts (palette closed / no items).
    const enterSend = e.key === "Enter" && !e.shiftKey && !composing;
    const cmdEnter = e.key === "Enter" && (e.metaKey || e.ctrlKey) && !composing;
    if (enterSend || cmdEnter) {
      e.preventDefault();
      if (!isPending && value.trim()) onSubmit();
    }
  };

  return (
    <div>
      <label id={labelId} htmlFor={`${listboxId}-input`} className="sr-only">
        {cs.composerLabel}
      </label>

      {showPalette ? (
        <ComposerPalette
          items={items}
          activeIndex={activeIndex}
          mode={mode}
          listboxId={listboxId}
          onSelect={selectItem}
          onHover={setActiveIndex}
        />
      ) : null}

      <div className="rounded-2xl border border-ink-700/80 bg-ink-900/75 p-2 shadow-lg shadow-ink-950/20 transition focus-within:border-accent-300/45 focus-within:ring-2 focus-within:ring-accent-300/15">
        <textarea
          id={`${listboxId}-input`}
          ref={ref}
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setDismissed(false);
          }}
          onKeyDown={onKeyDown}
          rows={1}
          role="combobox"
          aria-expanded={showPalette}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={
            showPalette && items.length > 0 ? paletteItemDomId(activeIndex) : undefined
          }
          aria-labelledby={labelId}
          aria-describedby={hintId}
          placeholder={placeholder ?? cs.placeholder}
          className="block max-h-40 min-h-[44px] w-full resize-none bg-transparent px-2.5 py-2 text-sm leading-6 text-ink-50 outline-none placeholder:text-ink-500"
        />
        <div className="flex items-center justify-between gap-3 border-t border-ink-800/70 px-1 pt-2">
          <button
            type="button"
            onClick={() => {
              setShowCaps((v) => !v);
              setDismissed(false);
              focusTextarea();
            }}
            className={[
              "btn-ghost !min-h-8 !px-2.5 text-xs",
              capsOpen ? "bg-accent-300/10 text-accent-200" : "text-ink-400",
            ].join(" ")}
            aria-pressed={capsOpen}
            title={cs.paletteInsertCap}
          >
            <Zap size={13} />
            {cs.advanced}
          </button>
          <div className="flex items-center gap-3">
            <span id={hintId} className="hidden text-[11px] text-ink-500 sm:inline">
              {hint ?? cs.hint}
            </span>
            {isPending ? (
              <button
                type="button"
                onClick={onStop}
                className="btn-danger !min-h-9 !w-9 !p-0"
                aria-label={cs.ariaStop}
                title={cs.ariaStop}
              >
                <StopCircle size={14} />
              </button>
            ) : (
              <button
                type="button"
                onClick={onSubmit}
                className="btn-primary !min-h-9 !w-9 !p-0"
                disabled={!value.trim()}
                aria-label={cs.ariaSend}
                title={cs.ariaSend}
              >
                <Send size={14} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
