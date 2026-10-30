import { useState, type KeyboardEvent } from "react";
import { useRuns, RUNS_LIMIT, type RunRow } from "@/lib/api";
import { FetchError } from "@/components/FetchError";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";
import { STATIONS, STATUS } from "@/lib/palette";
import { duration, median, when } from "@/lib/format";

/** A run recorded before run telemetry has 0 for both, not a 0s run. */
const measured = (r: RunRow) => r.duration_ms > 0 || r.turns > 0;

const sum = (rs: RunRow[], f: (r: RunRow) => number) => rs.reduce((a, r) => a + f(r), 0);

function Outcome({ r }: { r: RunRow }) {
  // The word always; the icon and its status hue are a second channel.
  return (
    <span title={r.detail ?? undefined}>
      {r.failed && <span aria-hidden style={{ color: STATUS.critical }}>✕ </span>}
      {r.outcome}
    </span>
  );
}

function Model({ r }: { r: RunRow }) {
  if (!r.model) return <span className="text-muted-foreground">—</span>;
  return (
    <span title={r.models.join(", ")}>
      {r.model_mismatch && <span aria-hidden style={{ color: STATUS.warning }}>⚠ </span>}
      <span className="font-mono text-xs">{r.model}</span>
      {r.models.length > 1 && (
        <span className="text-muted-foreground"> +{r.models.length - 1}</span>)}
      {r.model_mismatch && (
        <div className="text-xs text-muted-foreground">asked for {r.model_requested}</div>)}
    </span>
  );
}

/** Per-station totals. Always over every station, whatever the filter shows. */
function ByStation({ runs, pick, picked }: {
  runs: RunRow[]; pick: (s: string | null) => void; picked: string | null;
}) {
  const rows = STATIONS.map((s) => {
    const rs = runs.filter((r) => r.kind === s);
    const timed = rs.filter(measured);
    return {
      s, n: rs.length, failed: rs.filter((r) => r.failed).length,
      med: median(timed.map((r) => r.duration_ms / 1000)),
      total: sum(timed, (r) => r.duration_ms / 1000),
      turns: median(timed.map((r) => r.turns)),
      tokens: sum(rs, (r) => r.tokens), cost: sum(rs, (r) => r.cost_usd),
    };
  }).filter((r) => r.n > 0);
  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="text-base">By station</CardTitle></CardHeader>
      <CardContent>
        <Table>
          <TableHeader><TableRow>
            <TableHead>station</TableHead><TableHead>runs</TableHead>
            <TableHead>failed</TableHead><TableHead>median time</TableHead>
            <TableHead>agent time</TableHead><TableHead>median turns</TableHead>
            <TableHead>tokens</TableHead><TableHead>cost</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.s} role="button" tabIndex={0}
                        aria-pressed={picked === r.s}
                        onClick={() => pick(picked === r.s ? null : r.s)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault(); pick(picked === r.s ? null : r.s);
                          }
                        }}
                        className={`cursor-pointer tabular-nums ${picked === r.s ? "bg-muted" : ""}`}>
                <TableCell className="font-mono text-xs">{r.s}</TableCell>
                <TableCell>{r.n}</TableCell>
                <TableCell>{r.failed ? `${r.failed} (${Math.round(100 * r.failed / r.n)}%)` : "0"}</TableCell>
                <TableCell>{duration(r.med)}</TableCell>
                <TableCell>{duration(r.total)}</TableCell>
                <TableCell>{r.turns == null ? "—" : Math.round(r.turns)}</TableCell>
                <TableCell>{r.tokens.toLocaleString()}</TableCell>
                <TableCell>${r.cost.toFixed(2)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export function Runs({ days, onOpen }: {
  days: number; onOpen: (id: string, station?: string) => void;
}) {
  const { data, isError, error } = useRuns(days);
  const [station, setStation] = useState<string | null>(null);
  const [failedOnly, setFailedOnly] = useState(false);
  if (isError) return <FetchError what="agent runs" error={error} />;
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;

  const all = data.runs;
  if (all.length === 0) {
    return (
      <Card className="mt-4"><CardContent className="p-6 text-sm text-muted-foreground">
        No agent ran in the last {days} days.
      </CardContent></Card>
    );
  }
  const shown = all.filter((r) => (!station || r.kind === station)
                                  && (!failedOnly || r.failed));
  const unmeasured = shown.filter((r) => !measured(r)).length;
  const mismatched = shown.filter((r) => r.model_mismatch).length;
  const failed = shown.filter((r) => r.failed).length;
  const open = (r: RunRow) => r.open_id && onOpen(r.open_id, r.log ?? undefined);

  return (
    <div className="space-y-4 pt-4">
      <ByStation runs={all} pick={setStation} picked={station} />

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center gap-2 pb-2">
          <CardTitle className="mr-auto text-base">Runs</CardTitle>
          <div className="flex flex-wrap gap-1" role="group" aria-label="station">
            {[null, ...STATIONS].map((s) => (
              <button key={s ?? "all"} onClick={() => setStation(s)}
                      aria-pressed={station === s}
                      className={`rounded px-2 py-1 font-mono text-xs ${
                        station === s ? "bg-muted" : "text-muted-foreground"}`}>
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
          <p className="mb-2 text-xs text-muted-foreground">
            {shown.length} runs · {failed} failed
            · {duration(sum(shown, (r) => r.duration_ms / 1000))} of agent time
            · {sum(shown, (r) => r.tokens).toLocaleString()} tok
            · ${sum(shown, (r) => r.cost_usd).toFixed(2)}
            {mismatched > 0 && ` · ${mismatched} answered by a model other than the one requested`}
            {unmeasured > 0 && ` · ${unmeasured} recorded before run times were kept`}
          </p>
          {shown.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No runs match this filter.</p>
          ) : (
            <Table>
              <TableHeader><TableRow>
                <TableHead>when</TableHead><TableHead>ticket</TableHead>
                <TableHead>station</TableHead><TableHead>outcome</TableHead>
                <TableHead>time</TableHead><TableHead>turns</TableHead>
                <TableHead>model</TableHead><TableHead>tokens</TableHead>
                <TableHead>cost</TableHead>
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
                            className={`tabular-nums ${r.open_id ? "cursor-pointer" : ""}`}>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {when(r.ts)}</TableCell>
                    <TableCell className="max-w-64">
                      <div className="font-mono text-xs">{r.task_key ?? r.task_id}
                        {r.repo_key && <span className="ml-2 text-muted-foreground">{r.repo_key}</span>}
                      </div>
                      {r.title && <div className="truncate text-xs">{r.title}</div>}
                    </TableCell>
                    {/* The ledger id is what `deputy score NAME --run ID` takes. */}
                    <TableCell className="font-mono text-xs">{r.log ?? r.kind}
                      <span className="ml-1 text-muted-foreground">#{r.id}</span></TableCell>
                    <TableCell className="text-xs"><Outcome r={r} /></TableCell>
                    <TableCell>{measured(r) ? duration(r.duration_ms / 1000) : "—"}</TableCell>
                    <TableCell>{measured(r) ? r.turns : "—"}</TableCell>
                    <TableCell><Model r={r} /></TableCell>
                    <TableCell>{r.tokens.toLocaleString()}</TableCell>
                    <TableCell>${r.cost_usd.toFixed(2)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {all.length >= RUNS_LIMIT && (
            <p className="mt-2 text-xs text-muted-foreground">
              Showing the newest {RUNS_LIMIT} runs in this window; narrow the range for older ones.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
