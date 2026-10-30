import { useState, type KeyboardEvent } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip,
         XAxis, YAxis } from "recharts";
import { useScores, type ScoreCard, type ScoreResult, type ScoresData } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { FetchError } from "@/components/FetchError";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";
import { GRID, MUTED, SERIES, STATUS } from "@/lib/palette";
import { useColorMode } from "@/lib/useColorMode";
import { pct, when } from "@/lib/format";

// A line per scorer takes a series slot; past five the chart would have to
// invent a hue, so the rest are left to the table view.
const MAX_LINES = SERIES.light.length;

/** Every local day from the first to the last one seen. A day with no
 *  verdicts is still a tick on the axis, so a gap reads as a gap rather than
 *  two days drawn side by side. */
function dayRange(days: string[]): string[] {
  const seen = [...new Set(days)].sort();
  if (seen.length === 0) return [];
  const iso = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const [y, m, d] = seen[0].split("-").map(Number);
  const out: string[] = [];
  // Local noon, so a DST change never lands a step on the same date twice.
  for (let t = new Date(y, m - 1, d, 12); iso(t) <= seen[seen.length - 1];
       t = new Date(t.getFullYear(), t.getMonth(), t.getDate() + 1, 12)) {
    out.push(iso(t));
  }
  return out;
}

function Verdict({ r }: { r: ScoreResult }) {
  // The word always; the icon and its status hue are a second channel.
  if (r.label == null) return <span className="text-muted-foreground">judging…</span>;
  return (
    <span>
      {r.passed === true && <span aria-hidden style={{ color: STATUS.good }}>✓ </span>}
      {r.passed === false && <span aria-hidden style={{ color: STATUS.critical }}>✕ </span>}
      {r.label}
      {r.score != null && <span className="text-muted-foreground"> ({r.score})</span>}
    </span>
  );
}

function ScorerCard({ c, sample }: { c: ScoreCard | undefined; sample?: number }) {
  if (!c) return null;
  const range = c.low == null || c.high == null ? null
    : `${pct(c.low)}–${pct(c.high)}`;
  const unscored = [
    c.insufficient && `${c.insufficient} insufficient evidence`,
    c.errors && `${c.errors} judge errors`,
    c.pending && `${c.pending} being judged`,
  ].filter(Boolean).join(" · ");
  return (
    <Card>
      <CardContent className="space-y-1 p-4">
        <div className="font-mono text-xs">{c.scorer}</div>
        <div className="text-xs text-muted-foreground">
          {c.stations.join(", ")}{sample != null && sample < 100 && ` · ${sample}% sampled`}</div>
        <div className="pt-1 text-2xl font-semibold tabular-nums">{pct(c.rate)}</div>
        <div className="text-xs text-muted-foreground tabular-nums">
          {range ? `95% range ${range} · n=${c.n}` : "no scored runs yet"}</div>
        <div className="text-xs tabular-nums">
          {c.passed} passed · {c.failed} failed</div>
        {unscored && <div className="text-xs text-muted-foreground">{unscored}</div>}
        <div className="text-xs text-muted-foreground tabular-nums">
          judging cost ${c.cost_usd.toFixed(3)} · {c.tokens.toLocaleString()} tok</div>
      </CardContent>
    </Card>
  );
}

function Empty({ data }: { data: ScoresData }) {
  const none = data.configured.length === 0;
  return (
    <Card className="mt-4"><CardContent className="space-y-2 p-6 text-sm text-muted-foreground">
      {none ? (
        <>
          <p>No scorers are configured, so nothing is judged and nothing is spent.</p>
          <p>A scorer is a markdown file with a rubric, in{" "}
            <span className="font-mono">{data.scorers_dir}</span>. Starters ship in{" "}
            <span className="font-mono">examples/scorers/</span>. Try one against real
            runs with <span className="font-mono">deputy score NAME</span> before copying
            it in; the tick starts judging once it is there.</p>
        </>
      ) : (
        <p>{data.configured.length} scorer{data.configured.length === 1 ? "" : "s"} configured
          ({data.configured.map((c) => c.name).join(", ")}), but no run in this window has
          been judged yet. Judging happens in the triage window, a few runs per tick.</p>
      )}
    </CardContent></Card>
  );
}

export function Scorers({ days, onOpen }: {
  days: number; onOpen: (id: string, station?: string) => void;
}) {
  const { data, isError, error } = useScores(days);
  const m = useColorMode();
  const [scorer, setScorer] = useState<string | null>(null);
  const [failedOnly, setFailedOnly] = useState(false);
  if (isError) return <FetchError what="scores" error={error} />;
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;

  const errors = data.errors.length > 0 && (
    <Card className="mt-4"><CardContent className="space-y-1 p-4 text-sm">
      <div><span aria-hidden style={{ color: STATUS.warning }}>⚠ </span>
        {data.errors.length} scorer file{data.errors.length === 1 ? "" : "s"} could not
        be loaded and {data.errors.length === 1 ? "is" : "are"} skipped:</div>
      {data.errors.map((e) => <div key={e} className="font-mono text-xs">{e}</div>)}
    </CardContent></Card>
  );
  if (data.cards.length === 0) return <>{errors}<Empty data={data} /></>;

  const sample = Object.fromEntries(data.configured.map((c) => [c.name, c.sample_rate]));
  const names = data.cards.map((c) => c.scorer);
  const lined = names.slice(0, MAX_LINES);
  const daysSeen = dayRange(data.cards.flatMap((c) => c.trend.map((t) => t.day)));
  // Rate per scorer per day, null where that scorer judged nothing that day:
  // a gap in the line, never a dip to zero.
  const trend = daysSeen.map((day) => {
    const row: Record<string, string | number | null> = { day: day.slice(5) };
    for (const c of data.cards) {
      const t = c.trend.find((x) => x.day === day);
      const n = t ? t.passed + t.failed : 0;
      row[c.scorer] = n ? Math.round((100 * t!.passed) / n) : null;
    }
    return row;
  });
  const trendRows = data.cards.flatMap((c) => c.trend.map((t) =>
    [t.day, c.scorer, t.passed, t.failed, pct(t.passed / (t.passed + t.failed))]));

  const shown = data.results.filter((r) => (!scorer || r.scorer === scorer)
                                           && (!failedOnly || r.passed === false));
  const open = (r: ScoreResult) => r.open_id && onOpen(r.open_id, r.log ?? undefined);
  const axis = { stroke: MUTED, fontSize: 11, tickLine: false, axisLine: false };

  return (
    <div className="space-y-4 pt-4">
      {errors}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {data.cards.map((c) => <ScorerCard key={c.scorer} c={c} sample={sample[c.scorer]} />)}
      </div>

      <ChartFrame title="Pass rate by day"
        columns={["day", "scorer", "passed", "failed", "rate"]} rows={trendRows}>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={trend}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={40} domain={[0, 100]}
                   tickFormatter={(v: number) => `${v}%`} />
            <Tooltip formatter={(v) => (v == null ? "—" : `${v}%`)} />
            <Legend iconType="plainline" labelStyle={{ color: MUTED }} />
            {lined.map((name, i) => (
              <Line key={name} dataKey={name} stroke={SERIES[m][i]} strokeWidth={2}
                    dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
        {names.length > MAX_LINES && (
          <p className="mt-2 text-xs text-muted-foreground">
            The first {MAX_LINES} scorers are drawn; the table view has all {names.length}.</p>
        )}
      </ChartFrame>

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center gap-2 pb-2">
          <CardTitle className="mr-auto text-base">Judged runs</CardTitle>
          <div className="flex flex-wrap gap-1" role="group" aria-label="scorer">
            {[null, ...names].map((s) => (
              <button key={s ?? "all"} onClick={() => setScorer(s)}
                      aria-pressed={scorer === s}
                      className={`rounded px-2 py-1 font-mono text-xs ${
                        scorer === s ? "bg-muted" : "text-muted-foreground"}`}>
                {s ?? "all"}</button>
            ))}
          </div>
          <label className="flex items-center gap-1 text-xs text-muted-foreground">
            <input type="checkbox" checked={failedOnly}
                   onChange={(e) => setFailedOnly(e.target.checked)} />
            failed only
          </label>
        </CardHeader>
        <CardContent>
          {shown.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No verdicts match this filter.</p>
          ) : (
            <Table>
              <TableHeader><TableRow>
                <TableHead>run</TableHead><TableHead>ticket</TableHead>
                <TableHead>scorer</TableHead><TableHead>verdict</TableHead>
                <TableHead>why</TableHead><TableHead>cost</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {shown.map((r) => (
                  <TableRow key={r.id}
                            {...(r.open_id ? {
                              role: "button", tabIndex: 0, onClick: () => open(r),
                              onKeyDown: (e: KeyboardEvent) => {
                                if (e.key === "Enter" || e.key === " ") {
                                  e.preventDefault(); open(r);
                                }
                              },
                            } : {})}
                            className={`align-top tabular-nums ${r.open_id ? "cursor-pointer" : ""}`}>
                    <TableCell className="whitespace-nowrap text-xs">
                      <div className="text-muted-foreground">{when(r.run_ts)}</div>
                      <div className="font-mono">{r.log ?? r.station}
                        <span className="ml-1 text-muted-foreground">#{r.event_id}</span></div>
                    </TableCell>
                    <TableCell className="max-w-48">
                      <div className="font-mono text-xs">{r.task_key ?? r.task_id}
                        {r.repo_key && <span className="ml-2 text-muted-foreground">{r.repo_key}</span>}
                      </div>
                      {r.title && <div className="truncate text-xs">{r.title}</div>}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{r.scorer}</TableCell>
                    <TableCell className="whitespace-nowrap text-xs"><Verdict r={r} /></TableCell>
                    <TableCell className="min-w-64 whitespace-normal text-xs">
                      {r.reasoning ?? ""}</TableCell>
                    <TableCell className="text-xs">${r.cost_usd.toFixed(3)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        A pass rate counts only runs the judge could score. Insufficient evidence
        and judge errors are shown beside it, never inside it, and the 95% range
        is there because a dozen runs do not make a percentage. Runs are dated by
        when they ran, not when they were judged. Click a row to read the run's log.
      </p>
    </div>
  );
}
