import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { AlertTriangle, RefreshCw, RotateCcw } from "lucide-react";

/**
 * App-level error boundary.
 *
 * Catches render-time and lifecycle errors in the subtree, logs them, and
 * shows a recoverable fallback. Two recovery paths are offered:
 *   - "Try again" resets the boundary state so React re-attempts the render
 *     (useful when the error was transient, e.g. a stale query result).
 *   - "Reload page" performs a hard `location.reload()` for unrecoverable
 *     module/state corruption.
 *
 * Note: error boundaries only catch errors thrown during rendering, in
 * lifecycle methods, and in constructors of the tree below them. They do
 * NOT catch errors inside event handlers, async code, or SSR. Callers that
 * need those should handle them explicitly (e.g. React Query `onError`).
 */

interface ErrorBoundaryProps {
  children: ReactNode;
  /**
   * Optional custom fallback. Receives the caught error and a `reset`
   * callback that clears the boundary so the children can re-mount.
   */
  fallback?: (err: Error, reset: () => void) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface the failure to the console for local debugging / log capture.
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught an error:", error, info);
  }

  private reset = (): void => {
    this.setState({ error: null });
  };

  private reload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    const { error } = this.state;
    const { children, fallback } = this.props;

    if (error) {
      if (fallback) return fallback(error, this.reset);

      return (
        <div
          role="alert"
          aria-live="assertive"
          className="grid h-full w-full place-items-center px-6 py-10"
        >
          <div className="card brand-shadow-pink-sm w-full max-w-lg p-6">
            <div className="flex items-start gap-3">
              <span
                aria-hidden="true"
                className="grid h-10 w-10 shrink-0 place-items-center rounded-control border border-rose-300/35 bg-rose-500/10 text-rose-300"
              >
                <AlertTriangle size={20} />
              </span>
              <div className="min-w-0 flex-1">
                <h2 className="headline text-lg text-ink-50">
                  Something went wrong
                </h2>
                <p className="panel-title mt-1 text-rose-300">
                  unexpected error
                </p>
              </div>
            </div>

            <p className="mt-4 break-words text-sm leading-relaxed text-ink-200">
              {error.message || "An unexpected error occurred while rendering this view."}
            </p>

            <div className="mt-6 flex flex-wrap items-center gap-2.5">
              <button
                type="button"
                className="btn-primary inline-flex items-center gap-1.5"
                onClick={this.reset}
              >
                <RotateCcw size={15} />
                Try again
              </button>
              <button
                type="button"
                className="btn-ghost inline-flex items-center gap-1.5"
                onClick={this.reload}
              >
                <RefreshCw size={15} />
                Reload page
              </button>
            </div>
          </div>
        </div>
      );
    }

    return children;
  }
}

export default ErrorBoundary;
