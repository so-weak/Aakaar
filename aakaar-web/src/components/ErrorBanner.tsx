import { AlertCircle } from "lucide-react";

import { ApiError } from "@/api/client";

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  let message: string;
  if (error instanceof ApiError) message = error.detail || error.message;
  else if (error instanceof Error) message = error.message;
  else message = String(error);
  return (
    <div className="brand-shadow-pink-sm flex items-start gap-2 rounded-control border border-rose-300/35 bg-rose-950/50 px-3 py-2 text-sm text-rose-100">
      <AlertCircle size={16} className="mt-0.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}
