import { Card, CardContent } from "@/components/ui/card";
import { STATUS } from "@/lib/palette";

/** What a view shows when its fetch failed.
 *
 * `ChartFrame` already states the principle for an empty chart -- a blank box
 * is indistinguishable from a failed fetch, and this dashboard exists to
 * remove exactly that kind of ambiguity. The pages owe themselves the same
 * thing: React Query leaves `data` undefined once its retries are exhausted,
 * so a bare `if (!data) return "loading…"` spins forever against an API that
 * is down, and an empty board or an empty table reads as "nothing in flight"
 * when the truth is "nobody asked".
 *
 * The reason is shown, not swallowed: `api.ts`'s `get()` unwraps FastAPI's
 * `{"detail": ...}` body, and the operator-facing messages `serve` produces
 * (a missing database names its path) are worth reading.
 */
export function FetchError({ what, error }: { what: string; error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <Card role="alert" className="mt-4">
      <CardContent className="space-y-2 p-6">
        <div className="text-sm font-medium" style={{ color: STATUS.critical }}>
          ✕ Could not load {what}.
        </div>
        <p className="text-sm text-muted-foreground">
          This page is empty because the request failed, not because there is
          nothing to show. It will retry on the next poll.
        </p>
        <pre className="overflow-x-auto rounded border p-2 font-mono text-xs">
          {message}
        </pre>
      </CardContent>
    </Card>
  );
}
