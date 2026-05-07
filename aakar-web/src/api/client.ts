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
