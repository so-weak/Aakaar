// useRunEvents — live run-event stream over a WebSocket.
//
// Opens `${wsBase}/ws/runs/${runId}` where `wsBase` is derived from the API
// base (VITE_API_BASE or "/api"), turned into an absolute ws(s):// origin.
// The JWT is passed as the single Sec-WebSocket-Protocol value — the browser
// sends it in the handshake and the backend reads the bearer from there
// (the WebSocket API gives us no other way to attach auth headers).
//
// Events are accumulated in state, deduped by `sequence` and kept sorted, so
// re-renders always see a stable, ordered list even across reconnects. On an
// unexpected close the socket reconnects with bounded exponential backoff;
// when `runId`/`token` change or the component unmounts everything is closed
// and reset cleanly. With a null `runId` or `token` the hook is inert.

import { useEffect, useRef, useState } from "react";

// TODO(integration): import { RunEvent } from "@/api/types" once this hook is
// wired in. Kept local here so the file is self-contained and matches the
// existing RunEvent shape (sequence/node_id/kind/payload/at).
export interface RunEvent {
  sequence: number;
  node_id: string | null;
  kind: string;
  payload: unknown;
  at: string;
}

export interface UseRunEventsResult {
  events: RunEvent[];
  connected: boolean;
  error: string | null;
}

// Backoff bounds for reconnection (milliseconds).
const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 15_000;

/**
 * Resolve the API base (VITE_API_BASE or "/api") to an absolute ws(s):// URL
 * for the given path, swapping http(s) for ws(s). Relative bases ("/api") are
 * anchored to the current window origin.
 */
function resolveWsUrl(path: string): string {
  const apiBase =
    (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";
  // Anchor relative bases against the current origin so URL() always succeeds.
  const absolute = new URL(apiBase, window.location.origin);
  absolute.protocol = absolute.protocol === "https:" ? "wss:" : "ws:";
  // Join the base path with the ws path, collapsing any double slash.
  const basePath = absolute.pathname.replace(/\/+$/, "");
  absolute.pathname = `${basePath}${path}`;
  absolute.search = "";
  absolute.hash = "";
  return absolute.toString();
}

/**
 * Merge a single incoming event into the existing (sorted, deduped) list.
 * Returns the same array reference when the event is a duplicate so React can
 * skip the re-render.
 */
function mergeEvent(prev: RunEvent[], next: RunEvent): RunEvent[] {
  // Fast path: strictly newer than everything we have.
  const last = prev[prev.length - 1];
  if (!last || next.sequence > last.sequence) {
    return [...prev, next];
  }
  // Binary search for the insertion point / existing entry.
  let lo = 0;
  let hi = prev.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const seq = prev[mid].sequence;
    if (seq === next.sequence) {
      return prev; // duplicate — drop it, keep the array stable
    }
    if (seq < next.sequence) lo = mid + 1;
    else hi = mid - 1;
  }
  const out = prev.slice();
  out.splice(lo, 0, next);
  return out;
}

export function useRunEvents(
  runId: string | null,
  token: string | null,
): UseRunEventsResult {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Mutable refs that must not trigger re-renders / effect re-runs.
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  // Guards against state updates from a socket that has been torn down.
  const activeRef = useRef(true);

  useEffect(() => {
    // Reset accumulated state whenever the run/token identity changes; a fresh
    // run must not show events leaked from the previous one.
    setEvents([]);
    setConnected(false);
    setError(null);
    attemptRef.current = 0;
    activeRef.current = true;

    // Inert when we have nothing to connect with.
    if (!runId || !token) {
      return () => {
        activeRef.current = false;
      };
    }

    const wsUrl = resolveWsUrl(`/ws/runs/${encodeURIComponent(runId)}`);

    const clearReconnect = () => {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const connect = () => {
      if (!activeRef.current) return;

      let socket: WebSocket;
      try {
        // The browser sends `token` as the Sec-WebSocket-Protocol header; the
        // backend reads the JWT from there (no custom headers on WS).
        socket = new WebSocket(wsUrl, token ? [token] : undefined);
      } catch (err) {
        if (!activeRef.current) return;
        setConnected(false);
        setError(err instanceof Error ? err.message : "WebSocket failed to open");
        scheduleReconnect();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        if (!activeRef.current) return;
        attemptRef.current = 0; // successful connect resets the backoff
        setConnected(true);
        setError(null);
      };

      socket.onmessage = (ev: MessageEvent) => {
        if (!activeRef.current) return;
        let parsed: RunEvent;
        try {
          parsed = JSON.parse(ev.data as string) as RunEvent;
        } catch {
          // Ignore non-JSON / malformed frames rather than tearing down.
          return;
        }
        if (typeof parsed?.sequence !== "number") return;
        setEvents((prev) => mergeEvent(prev, parsed));
      };

      socket.onerror = () => {
        if (!activeRef.current) return;
        // onerror is followed by onclose; surface a message but let close drive
        // the reconnect so we don't double-schedule.
        setError((cur) => cur ?? "WebSocket connection error");
      };

      socket.onclose = (ev: CloseEvent) => {
        if (socketRef.current === socket) socketRef.current = null;
        if (!activeRef.current) return;
        setConnected(false);
        // 1000 (normal) and 1001 (going away) are clean — don't reconnect.
        if (ev.code === 1000 || ev.code === 1001) return;
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (!activeRef.current) return;
      clearReconnect();
      const attempt = attemptRef.current;
      attemptRef.current = attempt + 1;
      // Bounded exponential backoff with mild jitter.
      const backoff = Math.min(
        RECONNECT_BASE_MS * 2 ** attempt,
        RECONNECT_MAX_MS,
      );
      const delay = backoff / 2 + Math.random() * (backoff / 2);
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        connect();
      }, delay);
    };

    connect();

    return () => {
      // Tear down: stop callbacks from touching state, cancel any pending
      // reconnect, and close the live socket cleanly.
      activeRef.current = false;
      clearReconnect();
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        const { OPEN, CONNECTING } = WebSocket;
        if (socket.readyState === OPEN || socket.readyState === CONNECTING) {
          socket.close(1000, "client navigating away");
        }
      }
    };
  }, [runId, token]);

  return { events, connected, error };
}

export default useRunEvents;
