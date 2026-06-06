import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ArrowRight, Compass, X } from "lucide-react";

/**
 * Self-contained guided-tour system — no API, no shared state.
 *
 * Wrap the app (or any subtree) in <TourProvider>, then call useTour()
 * from anywhere beneath it to start/stop a tour. A <TourButton> is the
 * convenience trigger. While a tour is active the provider renders a
 * fixed full-screen dim overlay with a "spotlight" cutout over the
 * current step's target element, plus a tooltip card anchored near it.
 *
 * Target resolution is by CSS selector via document.querySelector; the
 * spotlight + tooltip rectangles recompute on resize/scroll. If a step's
 * target is missing, the tour skips past it (forward or backward in the
 * direction of travel) so a stale selector never wedges the tour.
 *
 * Themed entirely with design tokens (ink, accent, signal families); no
 * hard-coded colors, so all themes keep working.
 */

// ---------- public types -------------------------------------------------

export interface TourStep {
  /** CSS selector resolved with document.querySelector. */
  selector: string;
  title: string;
  body: string;
}

export interface TourApi {
  startTour: (steps: TourStep[]) => void;
  endTour: () => void;
  active: boolean;
}

// ---------- context ------------------------------------------------------

const TourContext = createContext<TourApi | null>(null);

export function useTour(): TourApi {
  const ctx = useContext(TourContext);
  if (!ctx) {
    throw new Error("useTour() must be used within a <TourProvider>.");
  }
  return ctx;
}

// ---------- geometry helpers ---------------------------------------------

interface Rect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const SPOT_PADDING = 8;
const TOOLTIP_WIDTH = 320;
const TOOLTIP_GAP = 14;
const VIEWPORT_MARGIN = 12;

function rectFromElement(el: Element): Rect {
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, width: r.width, height: r.height };
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

/**
 * Decide where the tooltip sits relative to the spotlight, then clamp it
 * to the viewport. Prefers below the target, falls back to above, else
 * pins to the right/left edge if neither fits.
 */
function placeTooltip(
  spot: Rect,
  tooltipHeight: number,
  vw: number,
  vh: number,
): { top: number; left: number; placement: "top" | "bottom" } {
  const spotBottom = spot.top + spot.height;
  const fitsBelow = spotBottom + TOOLTIP_GAP + tooltipHeight <= vh - VIEWPORT_MARGIN;
  const fitsAbove = spot.top - TOOLTIP_GAP - tooltipHeight >= VIEWPORT_MARGIN;

  const placement: "top" | "bottom" = fitsBelow || !fitsAbove ? "bottom" : "top";

  const rawTop =
    placement === "bottom"
      ? spotBottom + TOOLTIP_GAP
      : spot.top - TOOLTIP_GAP - tooltipHeight;

  // Center horizontally over the target, then clamp into the viewport.
  const rawLeft = spot.left + spot.width / 2 - TOOLTIP_WIDTH / 2;

  return {
    top: clamp(rawTop, VIEWPORT_MARGIN, Math.max(VIEWPORT_MARGIN, vh - tooltipHeight - VIEWPORT_MARGIN)),
    left: clamp(rawLeft, VIEWPORT_MARGIN, Math.max(VIEWPORT_MARGIN, vw - TOOLTIP_WIDTH - VIEWPORT_MARGIN)),
    placement,
  };
}

// ---------- provider -----------------------------------------------------

export function TourProvider({ children }: { children: ReactNode }) {
  const [steps, setSteps] = useState<TourStep[]>([]);
  const [index, setIndex] = useState(0);
  const [active, setActive] = useState(false);

  // Travel direction is tracked so a missing target is skipped *past*
  // (not bounced into) when navigating: forward keeps going forward.
  const directionRef = useRef<1 | -1>(1);

  const startTour = useCallback((next: TourStep[]) => {
    if (!next.length) return;
    setSteps(next);
    setIndex(0);
    directionRef.current = 1;
    setActive(true);
  }, []);

  const endTour = useCallback(() => {
    setActive(false);
    setSteps([]);
    setIndex(0);
  }, []);

  const api = useMemo<TourApi>(
    () => ({ startTour, endTour, active }),
    [startTour, endTour, active],
  );

  const goTo = useCallback(
    (next: number, dir: 1 | -1) => {
      directionRef.current = dir;
      if (next < 0 || next >= steps.length) {
        endTour();
        return;
      }
      setIndex(next);
    },
    [steps.length, endTour],
  );

  const next = useCallback(() => goTo(index + 1, 1), [goTo, index]);
  const back = useCallback(() => goTo(index - 1, -1), [goTo, index]);

  return (
    <TourContext.Provider value={api}>
      {children}
      {active && steps.length > 0 ? (
        <TourOverlay
          steps={steps}
          index={index}
          direction={directionRef.current}
          onNext={next}
          onBack={back}
          onSkip={endTour}
          onGoTo={(i) => goTo(i, i >= index ? 1 : -1)}
        />
      ) : null}
    </TourContext.Provider>
  );
}

// ---------- overlay ------------------------------------------------------

interface TourOverlayProps {
  steps: TourStep[];
  index: number;
  direction: 1 | -1;
  onNext: () => void;
  onBack: () => void;
  onSkip: () => void;
  onGoTo: (index: number) => void;
}

function TourOverlay({
  steps,
  index,
  direction,
  onNext,
  onBack,
  onSkip,
  onGoTo,
}: TourOverlayProps) {
  const step = steps[index];
  const [spot, setSpot] = useState<Rect | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const [tooltipHeight, setTooltipHeight] = useState(180);
  const [vw, setVw] = useState(() => window.innerWidth);
  const [vh, setVh] = useState(() => window.innerHeight);

  const total = steps.length;
  const isFirst = index === 0;
  const isLast = index === total - 1;

  // Resolve the current target's rect; if missing, skip in the travel
  // direction (forward by default, backward when arriving via Back).
  const measure = useCallback(() => {
    const el = document.querySelector(step.selector);
    if (!el) {
      setSpot(null);
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= total) {
        onSkip();
      } else if (direction === -1) {
        onBack();
      } else {
        onNext();
      }
      return;
    }
    setSpot(rectFromElement(el));
    setVw(window.innerWidth);
    setVh(window.innerHeight);
  }, [step.selector, index, direction, total, onBack, onNext, onSkip]);

  // Re-measure when the step changes, and on resize/scroll while open.
  useLayoutEffect(() => {
    measure();
  }, [measure]);

  useEffect(() => {
    const onChange = () => measure();
    window.addEventListener("resize", onChange);
    // capture:true catches scroll on nested scroll containers too.
    window.addEventListener("scroll", onChange, true);
    return () => {
      window.removeEventListener("resize", onChange);
      window.removeEventListener("scroll", onChange, true);
    };
  }, [measure]);

  // Bring the target into view if it is offscreen (then a scroll event
  // re-measures and the spotlight follows).
  useEffect(() => {
    const el = document.querySelector(step.selector);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
    }
  }, [step.selector]);

  // Track tooltip height so vertical placement stays accurate.
  useLayoutEffect(() => {
    if (tooltipRef.current) {
      setTooltipHeight(tooltipRef.current.offsetHeight);
    }
  }, [step.title, step.body, spot, vw, vh]);

  // Keyboard: ArrowRight/Left navigate, Esc skips.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onSkip();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        onNext();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        onBack();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onNext, onBack, onSkip]);

  // While a target is being resolved/skipped, render nothing visible.
  if (!spot) return null;

  const padded: Rect = {
    top: spot.top - SPOT_PADDING,
    left: spot.left - SPOT_PADDING,
    width: spot.width + SPOT_PADDING * 2,
    height: spot.height + SPOT_PADDING * 2,
  };

  const { top: ttTop, left: ttLeft, placement } = placeTooltip(
    padded,
    tooltipHeight,
    vw,
    vh,
  );

  const overlay = (
    <div
      className="fixed inset-0 z-[1000]"
      role="dialog"
      aria-modal="true"
      aria-label={`Guided tour: ${step.title}`}
    >
      {/* Dim layer with a spotlight cutout. The box-shadow projects the
          scrim outward from the (transparent) cutout, so only the target
          stays lit. Clicking the scrim skips the tour. */}
      <div
        className="absolute rounded-card ring-2 ring-inset ring-accent-300/70"
        style={{
          top: padded.top,
          left: padded.left,
          width: padded.width,
          height: padded.height,
          boxShadow:
            "0 0 0 9999px rgb(var(--signal-black) / 0.72), 0 0 0 1px rgb(var(--accent-300) / 0.4)",
          transition: "top 180ms ease, left 180ms ease, width 180ms ease, height 180ms ease",
        }}
        onClick={onSkip}
        aria-hidden="true"
      />

      {/* Tooltip card */}
      <div
        ref={tooltipRef}
        className="card absolute flex flex-col gap-3 p-4 text-ink-100 brand-shadow-cyan-md"
        style={{
          top: ttTop,
          left: ttLeft,
          width: TOOLTIP_WIDTH,
          transition: "top 180ms ease, left 180ms ease",
        }}
        // Stop scrim's click-to-skip from firing through the card.
        onClick={(e) => e.stopPropagation()}
      >
        {/* Caret pointing toward the target */}
        <span
          aria-hidden="true"
          className={[
            "absolute h-3 w-3 rotate-45 border-accent-300/40 bg-ink-900",
            placement === "bottom"
              ? "-top-1.5 border-l border-t"
              : "-bottom-1.5 border-b border-r",
          ].join(" ")}
          style={{
            left: clamp(
              padded.left + padded.width / 2 - ttLeft - 6,
              16,
              TOOLTIP_WIDTH - 28,
            ),
          }}
        />

        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <span
              className="grid h-7 w-7 shrink-0 place-items-center rounded-control bg-accent-300/15 text-accent-300 ring-1 ring-inset ring-accent-300/40"
              aria-hidden="true"
            >
              <Compass size={15} />
            </span>
            <h2
              id="tour-title"
              className="headline text-sm leading-tight text-ink-50"
            >
              {step.title}
            </h2>
          </div>
          <button
            type="button"
            onClick={onSkip}
            className="btn-ghost -mr-1.5 -mt-1.5 inline-flex h-7 w-7 items-center justify-center rounded-control p-0"
            aria-label="Skip tour"
            title="Skip tour (Esc)"
          >
            <X size={15} />
          </button>
        </div>

        <p className="text-sm leading-relaxed text-ink-200">{step.body}</p>

        {/* Step dots + counter */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-1.5" role="tablist" aria-label="Tour steps">
            {steps.map((_, i) => {
              const isCurrent = i === index;
              return (
                <button
                  key={i}
                  type="button"
                  role="tab"
                  aria-selected={isCurrent}
                  aria-label={`Go to step ${i + 1} of ${total}`}
                  onClick={() => onGoTo(i)}
                  className={[
                    "h-1.5 rounded-full transition-all duration-200",
                    isCurrent
                      ? "w-5 bg-accent-300"
                      : "w-1.5 bg-ink-600 hover:bg-ink-400",
                  ].join(" ")}
                />
              );
            })}
          </div>
          <span className="panel-title shrink-0 text-[10px]">
            {index + 1} of {total}
          </span>
        </div>

        {/* Controls */}
        <div className="flex items-center justify-between gap-2 pt-0.5">
          <button
            type="button"
            onClick={onSkip}
            className="btn btn-ghost px-2.5 py-1.5 text-xs"
          >
            Skip
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onBack}
              disabled={isFirst}
              className="btn btn-ghost inline-flex items-center gap-1.5 px-3 py-1.5 text-xs"
              aria-label="Previous step"
            >
              <ArrowLeft size={14} />
              Back
            </button>
            <button
              type="button"
              onClick={onNext}
              className="btn btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs"
              aria-label={isLast ? "Finish tour" : "Next step"}
            >
              {isLast ? "Done" : "Next"}
              {isLast ? null : <ArrowRight size={14} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );

  return createPortal(overlay, document.body);
}

// ---------- trigger button -----------------------------------------------

interface TourButtonProps {
  /** The tour to start when clicked. */
  steps: TourStep[];
  /** Visible label; pass "" to render an icon-only button. */
  label?: string;
  className?: string;
}

export function TourButton({ steps, label = "Take a tour", className }: TourButtonProps) {
  const { startTour } = useTour();
  const iconOnly = label.trim() === "";

  return (
    <button
      type="button"
      onClick={() => startTour(steps)}
      className={[
        "btn btn-ghost inline-flex items-center gap-2",
        iconOnly ? "h-9 w-9 justify-center p-0" : "px-3 py-2",
        className ?? "",
      ].join(" ")}
      aria-label={iconOnly ? "Take a tour" : undefined}
      title={iconOnly ? "Take a tour" : undefined}
    >
      <Compass size={16} className="shrink-0" />
      {iconOnly ? null : <span>{label}</span>}
    </button>
  );
}
