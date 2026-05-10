import { useEffect, useState } from "react";

/**
 * Fetch a managed-storage `aakar://` URI as a blob URL via the API
 * `/objects` endpoint, using the bearer token from sessionStorage.
 *
 * Why a hook (not just an `<img src>`): browsers don't send the
 * Authorization header on plain `<img>` requests, so we have to
 * materialize the response as a blob and feed an object URL into the
 * tag. The hook revokes the URL on unmount or when `uri` changes.
 *
 * `null` uri short-circuits to `{ src: null, ... }` so callers can use
 * a single hook even when there's nothing to show yet.
 */
export function useObjectBlob(uri: string | null): {
  src: string | null;
  blob: Blob | null;
  err: string | null;
} {
  const [src, setSrc] = useState<string | null>(null);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!uri) {
      setSrc(null);
      setBlob(null);
      setErr(null);
      return;
    }
    let cancelled = false;
    let blobUrl: string | null = null;
    (async () => {
      try {
        const token = sessionStorage.getItem("aakar.token") ?? "";
        const base = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";
        const res = await fetch(
          `${base}/objects?uri=${encodeURIComponent(uri)}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
        const b = await res.blob();
        blobUrl = URL.createObjectURL(b);
        if (!cancelled) {
          setBlob(b);
          setSrc(blobUrl);
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [uri]);

  return { src, blob, err };
}
