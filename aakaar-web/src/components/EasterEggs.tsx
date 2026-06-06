import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

/**
 * EasterEggs — purely cosmetic, mounted once at the app root.
 *
 * Three discoverable, tasteful eggs. None of them mutate app state, gate
 * behavior, or persist anything. They simply put a small toast on screen.
 *
 *  1. Konami code (↑↑↓↓←→←→BA) — "Founders' Vault" toast.
 *  2. Type "namaste" anywhere outside a form field — greeting toast.
 *  3. Triple-click the brand logo tile (.logo-tile) — build-info toast.
 *
 * Toasts auto-dismiss. Trigger again to re-show; cooldown prevents spam.
 */

const KONAMI: ReadonlyArray<string> = [
  "ArrowUp",
  "ArrowUp",
  "ArrowDown",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "ArrowLeft",
  "ArrowRight",
  "b",
  "a",
];

const NAMASTE_WORD = "namaste";

const TOAST_TTL_MS = 3500;
const COOLDOWN_MS = 1500;
const TRIPLE_CLICK_WINDOW_MS = 800;

type ToastShape = { id: number; node: ReactNode };

export function EasterEggs() {
  const [toast, setToast] = useState<ToastShape | null>(null);
  const lastShownAt = useRef<number>(0);
  const idRef = useRef(0);

  const show = (node: ReactNode) => {
    const now = Date.now();
    if (now - lastShownAt.current < COOLDOWN_MS) return;
    lastShownAt.current = now;
    idRef.current += 1;
    setToast({ id: idRef.current, node });
  };

  // ---- Konami + type-word detection (keyboard) -----------------------------
  useEffect(() => {
    let konamiBuf: string[] = [];
    let wordBuf = "";

    const isEditable = (el: EventTarget | null): boolean => {
      if (!(el instanceof HTMLElement)) return false;
      const tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (el.isContentEditable) return true;
      return false;
    };

    const onKey = (e: KeyboardEvent) => {
      // Konami: arrows + b + a. Match across the whole document, regardless
      // of focus — arrows in a search box would otherwise eat the egg.
      const k = e.key;
      konamiBuf.push(k);
      if (konamiBuf.length > KONAMI.length) konamiBuf.shift();
      if (
        konamiBuf.length === KONAMI.length &&
        konamiBuf.every((x, i) => x.toLowerCase() === KONAMI[i].toLowerCase())
      ) {
        konamiBuf = [];
        show(<KonamiToast />);
        return;
      }

      // Word buffer for "namaste". Skip when focus is in an editable field
      // so we don't trigger while the user is genuinely typing a name into a
      // form. Resets on any non-letter key.
      if (isEditable(e.target)) {
        wordBuf = "";
        return;
      }
      if (/^[a-zA-Z]$/.test(k)) {
        wordBuf = (wordBuf + k.toLowerCase()).slice(-NAMASTE_WORD.length);
        if (wordBuf === NAMASTE_WORD) {
          wordBuf = "";
          show(<NamasteToast />);
        }
      } else {
        wordBuf = "";
      }
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // ---- Triple-click on logo tile ------------------------------------------
  useEffect(() => {
    let count = 0;
    let firstClickAt = 0;

    const onClick = (e: MouseEvent) => {
      const target = e.target;
      if (!(target instanceof Element)) return;
      // Match the brand tile that wraps the MorphLogo. Class is stable
      // across themes and is set in Layout.tsx / Login.tsx.
      const tile = target.closest(".logo-tile");
      if (!tile) {
        count = 0;
        return;
      }
      const now = Date.now();
      if (now - firstClickAt > TRIPLE_CLICK_WINDOW_MS) {
        count = 1;
        firstClickAt = now;
      } else {
        count += 1;
      }
      if (count >= 3) {
        count = 0;
        show(<BuildInfoToast />);
      }
    };

    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, []);

  // ---- Auto-dismiss -------------------------------------------------------
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), TOAST_TTL_MS);
    return () => clearTimeout(t);
  }, [toast]);

  if (!toast) return null;

  return (
    <div
      // Sits above everything, never blocks pointer events; toast itself
      // is dismissable by click.
      className="pointer-events-none fixed bottom-6 right-6 z-[60]"
      aria-live="polite"
      role="status"
    >
      <button
        type="button"
        className="egg-toast pointer-events-auto group flex items-start gap-3 rounded-card border px-4 py-3 text-left shadow-lg backdrop-blur-md transition"
        style={{
          background: "var(--surface-elevated-bg, rgb(17 24 39 / 0.94))",
          borderColor: "rgb(var(--accent-500) / 0.45)",
          color: "rgb(var(--ink-100))",
          minWidth: "16rem",
          maxWidth: "22rem",
        }}
        onClick={() => setToast(null)}
        key={toast.id}
      >
        {toast.node}
      </button>
    </div>
  );
}

// ---- toast bodies ---------------------------------------------------------

function KonamiToast() {
  return (
    <>
      <span
        aria-hidden="true"
        className="grid h-9 w-9 shrink-0 place-items-center rounded-control"
        style={{
          background: "rgb(var(--accent-500) / 0.18)",
          color: "rgb(var(--accent-500))",
          border: "1px solid rgb(var(--accent-500) / 0.45)",
        }}
      >
        {/* Stylized "om" mark — small and inline, no external assets. */}
        <span className="font-display text-lg leading-none">ॐ</span>
      </span>
      <span className="flex flex-col">
        <span className="text-sm font-semibold">
          Founders' Vault · unlocked
        </span>
        <span className="text-[11px] leading-snug opacity-80">
          The seed-mantra acknowledges you. Click to dismiss.
        </span>
      </span>
    </>
  );
}

function NamasteToast() {
  return (
    <>
      <span
        aria-hidden="true"
        className="grid h-9 w-9 shrink-0 place-items-center rounded-control"
        style={{
          background: "rgb(var(--signal-cyan) / 0.16)",
          color: "rgb(var(--signal-cyan))",
          border: "1px solid rgb(var(--signal-cyan) / 0.32)",
        }}
      >
        <span className="font-display text-sm font-semibold leading-none">नम</span>
      </span>
      <span className="flex flex-col">
        <span className="text-sm font-semibold">Namaste, Sadhaka.</span>
        <span className="text-[11px] leading-snug opacity-80">
          May your yajnas reach siddhi.
        </span>
      </span>
    </>
  );
}

function BuildInfoToast() {
  // Vite injects MODE; everything else stays static. We deliberately do not
  // pull from the backend or any network — eggs must be fully client-side.
  const mode = (import.meta as unknown as { env?: { MODE?: string } }).env?.MODE ?? "dev";
  return (
    <>
      <span
        aria-hidden="true"
        className="grid h-9 w-9 shrink-0 place-items-center rounded-control font-mono text-[10px]"
        style={{
          background: "rgb(var(--ink-50) / 0.08)",
          color: "rgb(var(--ink-100))",
          border: "1px solid rgb(var(--ink-100) / 0.18)",
        }}
      >
        v1
      </span>
      <span className="flex flex-col">
        <span className="text-sm font-semibold">AAKAAR · the workshop of forms</span>
        <span className="text-[11px] font-mono leading-snug opacity-80">
          v1 · mode: {mode}
        </span>
      </span>
    </>
  );
}
