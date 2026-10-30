import { useOverview, useTasks, type Overview as OverviewData, type TaskRow } from "@/lib/api";
import { StatTile } from "@/components/StatTile";
import { StateBadge } from "@/components/StateBadge";
import { FetchError } from "@/components/FetchError";
import { STATUS } from "@/lib/palette";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

function ago(ts: number): string {
  if (!ts) return "never";
  const s = Math.floor(Date.now() / 1000) - ts;
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

/** A gap this long inside an open advance window means the runner is late. */
const STALE_AFTER_S = 3600;

/** Mirror of `pipeline._in_window`, deliberately including its limits.
 *
 * `"07:00-20:00"`, inclusive at both ends, compared against local wall-clock
 * time -- and, like the runner's own version, a range that wraps midnight
 * (`"22:00-02:00"`) simply never matches. Reproducing the bug is the point:
 * this indicator has to agree with the gate it reports on, and a cleverer
 * implementation here would make the tile disagree with what the runner
 * actually does.
 */
function inWindow(ranges: string[] | undefined, now: Date): boolean {
  const mins = now.getHours() * 60 + now.getMinutes();
  for (const range of ranges ?? []) {
    const [from, to] = range.split("-");
    if (!from || !to) continue;
    const [sh, sm] = from.split(":").map(Number);
    const [eh, em] = to.split(":").map(Number);
    if ([sh, sm, eh, em].some((n) => !Number.isFinite(n))) continue;
    const start = sh * 60 + sm;
    const end = eh * 60 + em;
    if (start <= mins && mins <= end) return true;
  }
  return false;
}

const DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"] as const;

/** Is the runner supposed to be ticking right now?
 *
 * Both halves of `Pipeline.tick`'s gate: the day-of-week list and the
 * `advance` window, with the same defaults the pipeline applies to an unset
 * key (`runner.days` -> Mon-Fri, `advance` -> all day). Without the day
 * check a Saturday would be graded as an outage every time; without the
 * window check the tile is a flat threshold that cries all night and stays
 * silent through a 45-minute gap at 11am, which is the one failure this
 * dashboard exists to catch.
 */
function advanceOpen(o: OverviewData, now: Date): boolean {
  const days = (o.days ?? []).map((d) => d.toLowerCase().slice(0, 3));
  if (days.length > 0 && !days.includes(DAYS[now.getDay()])) return false;
  return inWindow(o.windows?.advance ?? ["00:00-23:59"], now);
}

/** What a person has to do, in the order it unblocks the most: an answer or
 *  an approval restarts a ticket on the next tick, a blocked row needs a
 *  decision, and a failing run needs someone to read its log. Every stuck row
 *  lands in exactly one group, so the counts sum to the Stuck tile. */
const GROUPS = [
  { key: "answer", title: "Questions to answer", states: ["needs-input"],
    hint: "reply on the ticket; the next tick re-reads it" },
  { key: "approve", title: "Plans to approve", states: ["plan-review"],
    hint: "reply approved, or correct the plan" },
  { key: "blocked", title: "Blocked or off-limits", states: ["blocked", "human-only"],
    hint: "read the comment on the ticket; set AI State to new to retry" },
  { key: "look", title: "Runs that need a look", states: null,
    hint: "open the row for its error and log" },
] as const;

function group(rows: TaskRow[]): [(typeof GROUPS)[number], TaskRow[]][] {
  const out = GROUPS.map((g) => [g, [] as TaskRow[]] as [(typeof GROUPS)[number], TaskRow[]]);
  for (const r of rows) {
    const i = GROUPS.findIndex((g) => g.states?.some((s) => s === r.state));
    out[i === -1 ? GROUPS.length - 1 : i][1].push(r);
  }
  return out.filter(([, rs]) => rs.length > 0);
}

// A div, not a <button>: the PR link inside it is interactive, and a link
// nested in a button is invalid markup that screen readers announce badly.
function StuckRow({ r, onOpen }: { r: TaskRow; onOpen: (id: string) => void }) {
  return (
    <div role="button" tabIndex={0} onClick={() => onOpen(r.task_id)}
         onKeyDown={(e) => {
           if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(r.task_id); }
         }}
         className="flex w-full cursor-pointer items-start gap-3 py-2 text-left hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <StateBadge state={r.state} />
      <div className="min-w-0 flex-1">
        <div className="font-mono text-sm">{r.task_key}
          <span className="ml-2 text-muted-foreground">{r.repo_key}</span>
          {r.pr_url && (
            <a href={r.pr_url} target="_blank" rel="noreferrer"
               onClick={(e) => e.stopPropagation()}
               onKeyDown={(e) => e.stopPropagation()}
               className="ml-2 text-xs underline">PR #{r.pr_number}</a>)}
        </div>
        <div className="truncate text-sm">{r.title}</div>
        <div className="text-xs text-muted-foreground">{r.reason}</div>
        {r.last_error && (
          <div className="mt-1 truncate font-mono text-xs text-muted-foreground">
            {r.last_error}</div>)}
      </div>
    </div>
  );
}

export function Overview({ onOpen }: { onOpen: (id: string) => void }) {
  const ov = useOverview();
  const tasks = useTasks();
  if (ov.isError) return <FetchError what="the overview" error={ov.error} />;
  if (!ov.data) return <div className="p-6 text-muted-foreground">loading…</div>;
  const o = ov.data;
  const stuck = tasks.data?.stuck ?? [];

  // This is time since the last *recorded event*, not a heartbeat -- an idle
  // tick that finds no work records nothing, so a healthy-but-idle runner and
  // a dead one look identical past the threshold. The tile is labelled and
  // worded to match what it actually measures.
  //
  // But a gap only means anything while the runner is supposed to be ticking.
  // Graded against the advance gate, the tile is quiet outside working hours
  // (where a gap is expected) and speaks up inside them (where it is the
  // failure this dashboard exists to catch).
  const now = new Date();
  const open = advanceOpen(o, now);
  const gap = Math.floor(now.getTime() / 1000) - o.last_event_ts;
  const stale = open && gap > STALE_AFTER_S;

  const lastActivitySub = stale
    ? "stale — no activity in over 1h with the advance window open"
    : open
      ? `advance window open · ${o.runner_id} · ${o.mode}`
      : `advance window closed · ${o.runner_id} · ${o.mode}`;

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
                  sub={o.daily_budget_usd ? `budget $${o.daily_budget_usd.toFixed(2)}` : "no budget set"}
                  tone={o.daily_budget_usd && o.today.cost_usd >= o.daily_budget_usd ? STATUS.critical : undefined} />
        <StatTile label="Last activity" value={ago(o.last_event_ts)}
                  sub={lastActivitySub}
                  tone={stale ? STATUS.warning : undefined} />
      </div>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">Needs a human</CardTitle></CardHeader>
        <CardContent>
          {tasks.isError ? (
            // Not "nothing is stuck" -- nobody asked. Saying the good news on
            // a failed fetch is the worst thing this card could do.
            <div className="py-6 text-sm" style={{ color: STATUS.critical }}>
              ✕ Could not load the task list, so this is unknown, not empty.
              <div className="mt-1 font-mono text-xs text-muted-foreground">
                {tasks.error instanceof Error ? tasks.error.message : String(tasks.error)}
              </div>
            </div>
          ) : stuck.length === 0 ? (
            <div className="py-6 text-sm text-muted-foreground">
              Nothing is stuck. Every in-flight row is progressing on its own.
            </div>
          ) : (
            <div className="space-y-4">
              {group(stuck).map(([g, rows]) => (
                <section key={g.key}>
                  <div className="flex items-baseline gap-2">
                    <h3 className="text-sm font-medium">{g.title}</h3>
                    <span className="text-xs tabular-nums text-muted-foreground">{rows.length}</span>
                    <span className="text-xs text-muted-foreground">· {g.hint}</span>
                  </div>
                  <div className="divide-y">
                    {rows.map((r) => <StuckRow key={r.task_id} r={r} onOpen={onOpen} />)}
                  </div>
                </section>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
