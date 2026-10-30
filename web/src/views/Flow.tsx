import { type KeyboardEvent } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis }
  from "recharts";
import { useFlow, type FlowRow } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { FetchError } from "@/components/FetchError";
import { StatTile } from "@/components/StatTile";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";
import { GRID, MUTED, SERIES, STATUS } from "@/lib/palette";
import { useColorMode } from "@/lib/useColorMode";
import { duration, pct, when } from "@/lib/format";

/** States where the ticket sits until a person acts. */
const WAITING = new Set(["needs-input", "plan-review"]);

/** Touches a person made on one row. Plan *approvals* are gates by design
 *  and the AI reviewer's rounds are not a person, so neither counts. */
const touches = (r: FlowRow) =>
  r.clarifications + r.plan_rejections + r.fix_feedback + (r.human_commits ?? 0);

function Autonomous({ v }: { v: boolean | null }) {
  if (v == null) {
    return <span className="text-muted-foreground"
                 title="Recorded before commit tracking, or a fix pass with no cause">—</span>;
  }
  return v
    ? <span><span aria-hidden style={{ color: STATUS.good }}>✓ </span>yes</span>
    : <span>no</span>;
}

function Touch({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div title={hint}>
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}

export function Flow({ days, onOpen }: { days: number; onOpen: (id: string) => void }) {
  const { data, isError, error } = useFlow(days);
  const m = useColorMode();
  if (isError) return <FetchError what="flow" error={error} />;
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;

  const { rows, summary: s } = data;
  if (s.merged === 0) {
    return (
      <Card className="mt-4"><CardContent className="space-y-1 p-6 text-sm text-muted-foreground">
        <p>No PR merged in the last {days} days.</p>
        <p>Flow measures merged work only, so it fills in as rows reach done.
          What is in flight right now is on the Board.</p>
      </CardContent></Card>
    );
  }

  const nOf = (k: keyof typeof s.n) =>
    s.n[k] === s.merged ? `n=${s.n[k]}` : `n=${s.n[k]} of ${s.merged}`;
  const unknown = s.merged - s.autonomy.known;

  const byState = Object.entries(s.time_by_state)
    .filter(([, secs]) => secs > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([state, secs]) => ({
      state: WAITING.has(state) ? `${state} (human)` : state, secs,
      // Plotted in hours so the axis ticks land on round numbers; the
      // tooltip and the table carry the exact duration.
      hours: Math.round((secs / 3600) * 10) / 10,
    }));
  const stateTotal = byState.reduce((a, r) => a + r.secs, 0);
  const axis = { stroke: MUTED, fontSize: 11, tickLine: false, axisLine: false };
  const keyOpen = (id: string) => (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(id); }
  };

  return (
    <div className="space-y-4 pt-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Merged" value={String(s.merged)} sub={`PRs, last ${days} days`} />
        <StatTile label="Cycle time" value={duration(s.median.total)}
                  sub={`median, first touch to merge · ${nOf("total")}`} />
        <StatTile label="Waiting on people" value={pct(s.waiting_share)}
                  sub="of all cycle time: questions, plan approval, PR review" />
        <StatTile label="Autonomous" value={pct(s.autonomy.rate)}
                  sub={`${s.autonomy.autonomous} of ${s.autonomy.known} merged with no human fix or commit`
                       + (unknown ? ` · ${unknown} unknown` : "")} />
        <StatTile label="To PR" value={duration(s.median.to_pr)}
                  sub={`median, first touch to PR open · ${nOf("to_pr")}`} />
        <StatTile label="PR to first review" value={duration(s.median.pr_to_review)}
                  sub={`median, ready to first human review · ${nOf("pr_to_review")}`} />
        <StatTile label="Review to merge" value={duration(s.median.review_to_merge)}
                  sub={`median · ${nOf("review_to_merge")}`} />
        <StatTile label="Waiting, per PR" value={duration(s.median.waiting)}
                  sub={`median · ${nOf("waiting")}`} />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Human touches</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Touch label="questions answered" value={s.touches.clarifications}
                     hint="Times a ticket went back to triage after a clarification round" />
              <Touch label="plans sent back" value={s.touches.plan_rejections}
                     hint="Plans a person rejected or corrected" />
              <Touch label="fixes asked on the PR" value={s.touches.fix_feedback}
                     hint="Fix passes triggered by a human PR comment or review" />
              <Touch label="commits by a person" value={s.human_commits}
                     hint="Commits on merged PRs that this runner did not make" />
            </div>
            <div className="border-t pt-3">
              <div className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
                Rework with no person involved</div>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Touch label="reviewer sent back" value={s.touches.review_rounds}
                       hint="Review station verdicts of request_changes" />
                <Touch label="CI fix passes" value={s.touches.fix_ci}
                       hint="Fix passes triggered by a failing check" />
              </div>
            </div>
          </CardContent>
        </Card>

        {/* One measure over categories, so one hue and no legend: the axis
            names each state. "(human)" marks the two states only a person
            can move a ticket out of. */}
        <ChartFrame title="Where the time went"
          columns={["state", "time", "share"]}
          rows={byState.map((r) => [r.state, duration(r.secs), pct(r.secs / stateTotal)])}>
          <ResponsiveContainer width="100%" height={Math.max(160, byState.length * 30)}>
            <BarChart data={byState} layout="vertical">
              <CartesianGrid stroke={GRID[m]} horizontal={false} />
              <XAxis type="number" {...axis} tickFormatter={(v: number) => `${v}h`} />
              <YAxis type="category" dataKey="state" {...axis} width={150} />
              <Tooltip cursor={{ fill: "transparent" }}
                       formatter={(v) => [duration(Number(v) * 3600), "time"]} />
              <Bar dataKey="hours" fill={SERIES[m][0]} radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Cost per merged PR, by size</CardTitle></CardHeader>
        <CardContent>
          <Table>
            <TableHeader><TableRow>
              <TableHead>lines changed</TableHead><TableHead>PRs</TableHead>
              <TableHead>median cost</TableHead><TableHead>median tokens</TableHead>
              <TableHead>total cost</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {s.cost_by_size.map((b) => (
                <TableRow key={b.size} className="tabular-nums">
                  <TableCell className="font-mono text-xs">{b.size}</TableCell>
                  <TableCell>{b.n}</TableCell>
                  <TableCell>{b.median_cost_usd == null ? "—" : `$${b.median_cost_usd.toFixed(2)}`}</TableCell>
                  <TableCell>{b.median_tokens == null ? "—" : Math.round(b.median_tokens).toLocaleString()}</TableCell>
                  <TableCell>${b.total_cost_usd.toFixed(2)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-2 text-xs text-muted-foreground">
            Size is additions plus deletions on the PR as merged, as GitHub
            counts them. Cost is the row's own plan, implement, review and fix
            runs; triage covers the whole ticket and judging is overhead, so
            neither is charged to one PR. "unknown" is a PR merged before size
            was recorded.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">Merged PRs</CardTitle></CardHeader>
        <CardContent>
          <Table>
            <TableHeader><TableRow>
              <TableHead>ticket</TableHead><TableHead>merged</TableHead>
              <TableHead>total</TableHead><TableHead>to PR</TableHead>
              <TableHead>PR to review</TableHead><TableHead>review to merge</TableHead>
              <TableHead>waiting</TableHead><TableHead>touches</TableHead>
              <TableHead>autonomous</TableHead><TableHead>lines</TableHead>
              <TableHead>cost</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.task_id} role="button" tabIndex={0}
                          onClick={() => onOpen(r.task_id)} onKeyDown={keyOpen(r.task_id)}
                          className="cursor-pointer tabular-nums">
                  <TableCell className="max-w-64">
                    <div className="font-mono text-xs">{r.task_key ?? r.task_id}
                      {r.repo_key && <span className="ml-2 text-muted-foreground">{r.repo_key}</span>}
                      {r.pr_url && (
                        <a href={r.pr_url} target="_blank" rel="noreferrer"
                           onClick={(e) => e.stopPropagation()}
                           onKeyDown={(e) => e.stopPropagation()}
                           className="ml-2 underline">#{r.pr_number}</a>)}
                    </div>
                    {r.title && <div className="truncate text-xs">{r.title}</div>}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                    {when(r.done_at)}</TableCell>
                  <TableCell>{duration(r.total)}</TableCell>
                  <TableCell>{duration(r.to_pr)}</TableCell>
                  <TableCell>{duration(r.pr_to_review)}</TableCell>
                  <TableCell>{duration(r.review_to_merge)}</TableCell>
                  <TableCell>{duration(r.waiting)}</TableCell>
                  <TableCell title={`${r.clarifications} questions · ${r.plan_rejections} plans sent back`
                                    + ` · ${r.fix_feedback} PR fixes · ${r.human_commits ?? "?"} commits`}>
                    {touches(r)}{r.human_commits == null && "+?"}</TableCell>
                  <TableCell><Autonomous v={r.autonomous} /></TableCell>
                  <TableCell>{r.lines == null ? "—" : r.lines.toLocaleString()}</TableCell>
                  <TableCell>${r.cost_usd.toFixed(2)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        State times come from ticks, so they are only as precise as the cron
        interval; the first review time is GitHub's own. A merged PR is
        autonomous when no fix pass came from a PR comment and every commit on
        it is the runner's. Rows from before commit tracking show "—" and are
        left out of the rate rather than guessed.
      </p>
    </div>
  );
}
