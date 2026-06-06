// Thin fetch wrapper.
//
// - Reads the bearer token from a callback (set by AuthContext).
// - Throws ApiError on non-2xx so React Query can place results into `error`.
// - Decodes JSON when the response declares it; otherwise returns text.

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

let tokenGetter: () => string | null = () => null;
let onUnauthorized: () => void = () => {};

export function configureAuth(opts: {
  getToken: () => string | null;
  onUnauthorized: () => void;
}) {
  tokenGetter = opts.getToken;
  onUnauthorized = opts.onUnauthorized;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public detail: string,
  ) {
    super(message);
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE" | "PUT";
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

/**
 * Turn a Retry-After header (delta-seconds or an HTTP-date) into a short,
 * human phrase. Returns " shortly" when the value is missing or unparseable.
 */
function formatRetryAfter(value: string | null): string {
  if (!value) return " shortly";
  const seconds = Number(value);
  let waitSeconds: number | null = null;
  if (Number.isFinite(seconds)) {
    waitSeconds = seconds;
  } else {
    const when = Date.parse(value);
    if (!Number.isNaN(when)) {
      waitSeconds = Math.max(0, Math.round((when - Date.now()) / 1000));
    }
  }
  if (waitSeconds === null || waitSeconds <= 0) return " shortly";
  if (waitSeconds < 60) return ` in ${waitSeconds}s`;
  const minutes = Math.ceil(waitSeconds / 60);
  return ` in about ${minutes} min`;
}

export async function request<T = unknown>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const url = new URL(API_BASE + path, window.location.origin);
  if (opts.query) {
    for (const [k, v] of Object.entries(opts.query)) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
  }

  const headers: Record<string, string> = {
    Accept: "application/json",
    ...opts.headers,
  };
  const token = tokenGetter();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(url.toString().replace(window.location.origin, ""), {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });

  if (response.status === 401) {
    onUnauthorized();
  }

  // Rate limited — surface a clear, user-friendly message rather than a raw
  // "HTTP 429" string. Honor Retry-After (seconds or an HTTP-date) when sent.
  if (response.status === 429) {
    const retryAfter = response.headers.get("Retry-After");
    const hint = formatRetryAfter(retryAfter);
    const message = `Rate limited — please retry${hint}.`;
    throw new ApiError(message, 429, message);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }

  if (!response.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : text || response.statusText;
    throw new ApiError(`HTTP ${response.status}: ${detail}`, response.status, detail);
  }

  return parsed as T;
}
