import { useMemo, useState } from "react";
import { Camera, Maximize2, X } from "lucide-react";

import type { RunEvent } from "@/api/types";
import { useObjectBlob } from "@/lib/objectBlob";
import { formatISTTime } from "@/lib/datetime";

/**
 * Find the most recent live_screen event in the event stream.
 * Returns null when there is none yet (run hasn't reached a browser
 * node, live_screenshots is disabled, or the run pre-dates this feature).
 */
export function findLatestLiveScreen(
  events: RunEvent[] | undefined,
): { uri: string; node_id: string | null; at: string } | null {
  if (!events || events.length === 0) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "live_screen" && typeof e.payload?.uri === "string") {
      return {
        uri: e.payload.uri,
        node_id: e.node_id,
        at: e.at,
      };
    }
  }
  return null;
}

interface LiveScreenPanelProps {
  events: RunEvent[];
  /** "panel" = full panel with header (RunDetail); "thumb" = compact thumbnail (live tile). */
  variant?: "panel" | "thumb";
  /** Optional: max height in px when panel mode. */
  maxHeight?: number;
}

export function LiveScreenPanel({
  events,
  variant = "panel",
  maxHeight,
}: LiveScreenPanelProps) {
  const latest = useMemo(() => findLatestLiveScreen(events), [events]);
  const { src, err } = useObjectBlob(latest?.uri ?? null);
  const [enlarged, setEnlarged] = useState(false);

  if (variant === "thumb") {
    if (!latest || !src) return null;
    return (
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setEnlarged(true);
        }}
        className="absolute bottom-2 right-2 h-16 w-24 overflow-hidden rounded-md border-2 border-ink-700 bg-ink-950 shadow-[0_0_12px_rgb(0_0_0/0.6)] transition hover:border-accent-300/70"
        title="Live screen — click to enlarge"
      >
        <img
          src={src}
          alt=""
          className="h-full w-full object-cover object-top"
        />
        <span className="pointer-events-none absolute left-1 top-1 rounded bg-ink-950/85 px-1 font-mono text-[8px] uppercase tracking-wider text-signal-cyan">
          live
        </span>
        {enlarged ? <Lightbox src={src} onClose={() => setEnlarged(false)} /> : null}
      </button>
    );
  }

  // Panel variant
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-ink-700/80 bg-ink-950/45 px-6 py-3">
        <div className="flex items-center gap-2 panel-title">
          <Camera size={11} className="text-signal-cyan" />
          Live screen
          {latest ? (
            <span className="ml-1 font-mono text-[10px] normal-case tracking-normal text-ink-500">
              · last seen at {formatISTTime(latest.at)} IST
              {latest.node_id ? (
                <>
                  {" "}
                  · <span className="text-ink-300">{latest.node_id}</span>
                </>
              ) : null}
            </span>
          ) : null}
        </div>
        {src ? (
          <button
            type="button"
            className="btn-ghost"
            onClick={() => setEnlarged(true)}
            title="Enlarge"
          >
            <Maximize2 size={13} />
          </button>
        ) : null}
      </div>
      <div
        className="relative flex-1 overflow-hidden bg-ink-950/40"
        style={maxHeight ? { maxHeight } : undefined}
      >
        {err ? (
          <div className="grid h-full place-items-center px-6 text-center text-sm text-rose-300">
            Couldn’t load live screen: {err}
          </div>
        ) : !latest ? (
          <div className="grid h-full place-items-center px-6 text-center">
            <div>
              <Camera
                size={28}
                className="mx-auto mb-2 text-ink-700"
                aria-hidden
              />
              <div className="text-sm text-ink-400">
                No live screen yet — waiting for the first browser step.
              </div>
              <div className="mt-1 font-mono text-[10px] uppercase tracking-[0.22em] text-ink-600">
                requires AAKAR_LIVE_SCREENSHOTS=true
              </div>
            </div>
          </div>
        ) : !src ? (
          <div className="grid h-full place-items-center text-xs text-ink-500">
            Loading…
          </div>
        ) : (
          <img
            src={src}
            alt="Live browser screen"
            className="h-full w-full object-contain"
          />
        )}
      </div>
      {enlarged && src ? (
        <Lightbox src={src} onClose={() => setEnlarged(false)} />
      ) : null}
    </div>
  );
}

function Lightbox({ src, onClose }: { src: string; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-ink-950/90 p-8 backdrop-blur"
      onClick={onClose}
    >
      <button
        type="button"
        className="btn-ghost absolute right-6 top-6"
        onClick={onClose}
      >
        <X size={16} />
      </button>
      <img
        src={src}
        alt="Live browser screen (enlarged)"
        className="max-h-full max-w-full rounded-md border border-ink-700 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      />
    </div>
  );
}
