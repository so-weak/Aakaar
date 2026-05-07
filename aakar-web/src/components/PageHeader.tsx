import type { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="relative z-10 flex items-end justify-between gap-4 border-b border-ink-700/80 bg-ink-950/45 px-7 py-5 backdrop-blur-xl">
      <div className="min-w-0">
        <div className="stamp mb-3">console</div>
        <h1 className="truncate text-2xl font-black uppercase tracking-wide text-ink-50">
          {title}
        </h1>
        {subtitle ? <p className="mt-1 max-w-4xl text-sm leading-6 text-ink-300">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}
