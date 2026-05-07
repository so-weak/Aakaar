import type { ReactNode } from "react";

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="grid flex-1 place-items-center px-8 py-16">
      <div className="card max-w-md px-8 py-7 text-center">
        <div className="mx-auto mb-4 h-1 w-16 rounded-full bg-accent-300 shadow-[20px_0_0_rgb(255_59_147),-20px_0_0_rgb(22_217_255)]" />
        <h2 className="text-lg font-black uppercase tracking-wide text-ink-50">{title}</h2>
        <p className="mt-2 text-sm leading-6 text-ink-300">{description}</p>
        {action ? <div className="mt-5">{action}</div> : null}
      </div>
    </div>
  );
}
