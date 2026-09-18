import { useTasks, type TaskRow } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";

// Graph order, so the board reads left to right the way the pipeline runs.
const COLUMNS = ["new", "planning", "plan-review", "queued",
                 "in-progress", "review", "pr-open"] as const;

export function Board({ onOpen }: { onOpen: (id: string) => void }) {
  const { data } = useTasks();
  const rows = data?.tasks ?? [];
  // A repo with an active row is locked: graph.repo_is_free made visible
  // rather than inferred. The marker belongs on the OTHER cards in that
  // repo -- the ones actually waiting on the lock -- not on the in-progress
  // card itself, which is the lock holder, not something blocked by it.
  const locked = new Set(rows.filter((r) => r.state === "in-progress")
                             .map((r) => r.repo_key));
  return (
    <div className="grid gap-3 pt-4" style={{ gridTemplateColumns: `repeat(${COLUMNS.length}, minmax(9rem, 1fr))` }}>
      {COLUMNS.map((col) => {
        const inCol = rows.filter((r) => r.state === col);
        return (
          <div key={col} className="space-y-2">
            <div className="flex items-center justify-between text-xs uppercase tracking-wide text-muted-foreground">
              <span>{col}</span><span className="tabular-nums">{inCol.length}</span>
            </div>
            {inCol.map((r: TaskRow) => (
              <Card key={r.task_id} onClick={() => onOpen(r.task_id)}
                    role="button" tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") onOpen(r.task_id);
                      if (e.key === " ") { e.preventDefault(); onOpen(r.task_id); }
                    }}
                    className="cursor-pointer hover:border-foreground/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <CardContent className="space-y-1 p-3">
                  <div className="font-mono text-xs">{r.task_key}</div>
                  <div className="line-clamp-2 text-sm">{r.title}</div>
                  <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
                    <span>{r.repo_key}</span>
                    {locked.has(r.repo_key) && r.state !== "in-progress" && <span>· repo busy</span>}
                    {r.attempts > 0 && <span>· {r.attempts} attempts</span>}
                    {r.tokens > 0 && <span>· {r.tokens.toLocaleString()} tok</span>}
                  </div>
                  {r.pr_url && (
                    <a href={r.pr_url} target="_blank" rel="noreferrer"
                       tabIndex={0}
                       onClick={(e) => e.stopPropagation()}
                       onKeyDown={(e) => e.stopPropagation()}
                       className="text-xs underline">PR #{r.pr_number}</a>)}
                </CardContent>
              </Card>
            ))}
          </div>
        );
      })}
    </div>
  );
}
