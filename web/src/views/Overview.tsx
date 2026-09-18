import { useOverview, useTasks } from "@/lib/api";
import { StatTile } from "@/components/StatTile";
import { StateBadge } from "@/components/StateBadge";
import { STATUS } from "@/lib/palette";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

function ago(ts: number): string {
  if (!ts) return "never";
  const s = Math.floor(Date.now() / 1000) - ts;
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function Overview({ onOpen }: { onOpen: (id: string) => void }) {
  const ov = useOverview();
  const tasks = useTasks();
  if (!ov.data) return <div className="p-6 text-muted-foreground">loading…</div>;
  const o = ov.data;
  const stuck = tasks.data?.stuck ?? [];

  // A runner that has not ticked during an open window is the failure this
  // dashboard exists to catch, and nothing else on the page would show it.
  const silent = Math.floor(Date.now() / 1000) - o.last_event_ts > 3600;

  return (
    <div className="space-y-4 pt-4">
      <div className="grid gap-3 grid-cols-2 md:grid-cols-5">
        <StatTile label="Stuck" value={String(o.stuck)}
                  tone={o.stuck ? STATUS.critical : STATUS.good}
                  sub={o.stuck ? "needs a human" : "all clear"} />
        <StatTile label="Active" value={`${o.active} / ${o.max_active_tasks}`}
                  sub="slots in use" />
        <StatTile label="Tokens today" value={o.today.tokens.toLocaleString()}
                  sub={o.daily_tokens ? `cap ${o.daily_tokens.toLocaleString()}` : "no cap set"}
                  tone={o.daily_tokens && o.today.tokens >= o.daily_tokens ? STATUS.critical : undefined} />
        <StatTile label="Spend today" value={`$${o.today.cost_usd.toFixed(2)}`}
                  sub={`budget $${o.daily_budget_usd.toFixed(2)}`}
                  tone={o.today.cost_usd >= o.daily_budget_usd ? STATUS.critical : undefined} />
        <StatTile label="Last tick" value={ago(o.last_event_ts)}
                  sub={`${o.runner_id} · ${o.mode}`}
                  tone={silent ? STATUS.warning : undefined} />
      </div>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">Needs a human</CardTitle></CardHeader>
        <CardContent>
          {stuck.length === 0 ? (
            <div className="py-6 text-sm text-muted-foreground">
              Nothing is stuck. Every in-flight row is progressing on its own.
            </div>
          ) : (
            <div className="divide-y">
              {stuck.map((r) => (
                <button key={r.task_id} onClick={() => onOpen(r.task_id)}
                        className="flex w-full items-start gap-3 py-2 text-left hover:bg-muted/50">
                  <StateBadge state={r.state} />
                  <div className="min-w-0 flex-1">
                    <div className="font-mono text-sm">{r.task_key}
                      <span className="ml-2 text-muted-foreground">{r.repo_key}</span></div>
                    <div className="truncate text-sm">{r.title}</div>
                    <div className="text-xs text-muted-foreground">{r.reason}</div>
                    {r.last_error && (
                      <div className="mt-1 truncate font-mono text-xs text-muted-foreground">
                        {r.last_error}</div>)}
                  </div>
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
