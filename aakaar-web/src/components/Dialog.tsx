// Dialog — one accessible modal primitive for the whole app.
//
// Replaces ad-hoc `fixed inset-0` overlays (and the jarring native
// window.confirm) with a single implementation that gets the a11y contract
// right: role="dialog" aria-modal, a labelled title, a focus trap (Tab/Shift+Tab
// cycle inside), Escape-to-close, focus moved in on open and RESTORED to the
// trigger on close, and a guarded backdrop click. Styling stays token-driven
// via the shared `.card` class so every theme reskins it.

import { useCallback, useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  /** Optional supporting line announced with the title. */
  description?: ReactNode;
  children: ReactNode;
  /** Footer actions (buttons). Rendered right-aligned. */
  footer?: ReactNode;
  /** Tone accents the header icon / border. */
  tone?: "default" | "danger";
  icon?: ReactNode;
  /** When false, clicking the backdrop does not close (e.g. while saving). */
  dismissable?: boolean;
  /** Constrain width; defaults to a comfortable form width. */
  maxWidthClassName?: string;
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  tone = "default",
  icon,
  dismissable = true,
  maxWidthClassName = "max-w-md",
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descId = useId();

  const requestClose = useCallback(() => {
    if (dismissable) onClose();
  }, [dismissable, onClose]);

  // Move focus in on open, restore it on close/unmount.
  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    // Prefer an [autofocus] target, else the first focusable, else the panel.
    const auto = panel?.querySelector<HTMLElement>("[data-autofocus]");
    const first = panel?.querySelector<HTMLElement>(FOCUSABLE);
    (auto ?? first ?? panel)?.focus();
    return () => {
      previouslyFocused.current?.focus?.();
    };
  }, [open]);

  // Escape to close + Tab focus trap, scoped to while the dialog is open.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        requestClose();
        return;
      }
      if (e.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [open, requestClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 px-4 backdrop-blur-md"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) requestClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={[
          "card w-full p-6 shadow-2xl outline-none",
          maxWidthClassName,
          tone === "danger" ? "border-rose-500/25" : "",
        ].join(" ")}
      >
        <div className="flex items-start gap-3">
          {icon ? (
            <span
              className={[
                "mt-0.5 grid h-10 w-10 shrink-0 place-items-center rounded-xl",
                tone === "danger"
                  ? "bg-rose-500/10 text-rose-300"
                  : "bg-accent-300/10 text-accent-200",
              ].join(" ")}
            >
              {icon}
            </span>
          ) : null}
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-base font-semibold text-ink-50">
              {title}
            </h2>
            {description ? (
              <p id={descId} className="mt-1 text-sm leading-6 text-ink-300">
                {description}
              </p>
            ) : null}
          </div>
        </div>

        {children ? <div className="mt-4">{children}</div> : null}

        {footer ? <div className="mt-6 flex justify-end gap-2">{footer}</div> : null}
      </div>
    </div>
  );
}
