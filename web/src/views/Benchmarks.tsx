import { useState, type KeyboardEvent } from "react";
import { useBench, type BenchMiss, type BenchRun, type BenchVariant } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { Command } from "@/components/Command";
import { FetchError } from "@/components/FetchError";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";
import { GRID, SERIES, STATUS, SURFACE } from "@/lib/palette";
import { useColorMode } from "@/lib/useColorMode";
import { pct, when } from "@/lib/format";

type Open = (id: string, station?: string) => void;

const usd = (v: number) => `$${v.toFixed(4)}`;

/** "sonnet · builtin@1a2b3c4d" -> "sonnet · builtin": the hash is for telling
 *  two edits of one file apart, and the table view and tooltip keep it. */
const short = (label: string) => label.replace(/@[0-9a-f]{8}/, "");

function Status({ run }: { run: BenchRun }) {
  if (run.finished == null) {
    return <span><span aria-hidden style={{ color: STATUS.warning }}>⚠ </span>
      did not finish</span>;
  }
  if (run.note) {
    return <span><span aria-hidden style={{ color: STATUS.warning }}>⚠ </span>
      {run.note}</span>;
  }
  return <span>finished</span>;
}

/** One variant per row: accuracy with its 95% range on one axis, cost per
 *  call on another. Two measures, two scales, so two tracks -- never one
 *  chart with two y-axes. Plain HTML rather than a chart library, so a long
 *  variant name wraps above its tracks on a phone instead of being cut. */
function VariantRows({ variants }: { variants: BenchVariant[] }) {
  const m = useColorMode();
  const hue = SERIES[m][0];
  const maxCost = Math.max(...variants.map((v) => v.avg_cost_usd), 0);
  const tick = (left: number) => (
    <div className="absolute inset-y-0 w-px" style={{ left: `${left}%`, background: GRID[m] }} />
  );
  return (
    <div className="space-y-3">
      <div className="hidden grid-cols-[minmax(0,14rem)_1fr_10rem] gap-4 text-xs
                      text-muted-foreground md:grid">
        <span>variant</span>
        <span className="flex justify-between"><span>0%</span>
          <span>accuracy against history, with 95% range</span><span>100%</span></span>
        <span>average cost per call</span>
      </div>
      <p className="text-xs text-muted-foreground md:hidden">
        Each variant: accuracy against history on a 0–100% track, with its 95%
        range, then its average cost per call.</p>
      {variants.map((v) => {
        const range = v.low == null || v.high == null ? null
          : `${pct(v.low)}–${pct(v.high)}`;
        const said = v.accuracy == null ? "no graded calls"
          : `${v.correct}/${v.n} · ${pct(v.accuracy)}${range ? ` (${range})` : ""}`;
        return (
          <div key={v.variant} title={`${v.variant}\n${said}\n${usd(v.avg_cost_usd)} a call`}
               className="grid gap-x-4 gap-y-1 md:grid-cols-[minmax(0,14rem)_1fr_10rem]
                          md:items-center">
            <div className="min-w-0 break-words font-mono text-xs">{short(v.variant)}</div>
            <div>
              <div className="relative h-6" role="img"
                   aria-label={`${short(v.variant)}: accuracy ${said}`}>
                {[0, 25, 50, 75, 100].map((t) => <span key={t}>{tick(t)}</span>)}
                {v.low != null && v.high != null && (
                  <div className="absolute top-1/2 h-0.5 -translate-y-1/2"
                       style={{ left: `${v.low * 100}%`,
                                width: `${(v.high - v.low) * 100}%`, background: hue }} />
                )}
                {v.accuracy != null && (
                  <div className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2
                                  rounded-full"
                       style={{ left: `${v.accuracy * 100}%`, background: hue,
                                boxShadow: `0 0 0 2px ${SURFACE[m]}` }} />
                )}
              </div>
              <div className="text-xs tabular-nums text-muted-foreground">{said}
                {v.errors > 0 && ` · ${v.errors} failed`}</div>
            </div>
            <div>
              <div className="relative h-6">
                <div className="absolute inset-y-1.5 left-0 rounded-r"
                     style={{ width: maxCost ? `${(v.avg_cost_usd / maxCost) * 100}%` : 0,
                              minWidth: v.avg_cost_usd ? 2 : 0, background: hue }} />
              </div>
              <div className="text-xs tabular-nums text-muted-foreground">
                {usd(v.avg_cost_usd)} · {Math.round(v.avg_tokens).toLocaleString()} tok</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** A decision as the pipeline would act on it: repos only matter to execute,
 *  and `_pick_repos` fills them in for the other two as well. */
function said(decision: string | null, repos: string[]) {
  if (decision == null) return "failed";
  return decision === "execute" && repos.length ? `execute → ${repos.join(", ")}` : decision;
}

function Misses({ run, onOpen }: { run: BenchRun; onOpen: Open }) {
  const [variant, setVariant] = useState<string | null>(null);
  const names = [...new Set(run.misses.map((x) => x.variant))];
  const shown = run.misses.filter((x) => !variant || x.variant === variant);
  const open = (x: BenchMiss) => x.open_id && onOpen(x.open_id);
  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center gap-2 pb-2">
        <CardTitle className="mr-auto text-base">Where a variant disagreed with history</CardTitle>
        {names.length > 1 && (
          <div className="flex flex-wrap gap-1" role="group" aria-label="variant">
            {[null, ...names].map((n) => (
              <button key={n ?? "all"} onClick={() => setVariant(n)}
                      aria-pressed={variant === n}
                      className={`rounded px-2 py-1 font-mono text-xs ${
                        variant === n ? "bg-muted" : "text-muted-foreground"}`}>
                {n ? short(n) : "all"}</button>
            ))}
          </div>
        )}
      </CardHeader>
      <CardContent>
        {shown.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            Every graded call agreed with history.</p>
        ) : (
          <Table>
            <TableHeader><TableRow>
              <TableHead>ticket</TableHead><TableHead>history</TableHead>
              <TableHead>variant said</TableHead><TableHead>its reasoning</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {shown.map((x) => (
                <TableRow key={x.id}
                          {...(x.open_id ? {
                            role: "button", tabIndex: 0, onClick: () => open(x),
                            onKeyDown: (e: KeyboardEvent) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault(); open(x);
                              }
                            },
                          } : {})}
                          className={`align-top ${x.open_id ? "cursor-pointer" : ""}`}>
                  <TableCell className="max-w-48">
                    <div className="font-mono text-xs">{x.task_key ?? x.case}</div>
                    {x.title && <div className="truncate text-xs">{x.title}</div>}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs">
                    <div>{x.correct === 1 && x.routed === 0
                      ? said("execute", x.expected_repos ?? [])
                      : x.expected.join(" or ")}</div>
                    <div className="text-muted-foreground">{x.why}
                      {x.recorded && ` · said ${x.recorded} then`}</div>
                  </TableCell>
                  <TableCell className="text-xs">
                    <div className="whitespace-nowrap">
                      <span aria-hidden style={{ color: STATUS.critical }}>✕ </span>
                      {said(x.decision, x.repos)}</div>
                    <div className="font-mono text-muted-foreground">{short(x.variant)}</div>
                  </TableCell>
                  <TableCell className="min-w-64 whitespace-normal text-xs">
                    {x.reasoning ?? ""}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function Empty() {
  return (
    <Card className="mt-4"><CardContent className="space-y-3 p-6 text-sm text-muted-foreground">
      <p>No benchmark runs in this window. A benchmark replays tickets triage has
        already seen, under another model or prompt, and grades each call against
        what happened to the ticket afterwards. Nothing runs on its own: it is a
        command you run when you are weighing a change.</p>
      <p>See what history can grade, and what a run would cost, without calling anything:</p>
      <Command text="deputy bench triage --dry-run" />
      <p>Then try a model, or a prompt you edited from the built-in one:</p>
      <Command text="deputy bench triage --model haiku" />
      <Command text="deputy bench prompt > careful.md" />
      <Command text="deputy bench triage --prompt careful.md" />
    </CardContent></Card>
  );
}

export function Benchmarks({ days, onOpen }: { days: number; onOpen: Open }) {
  const { data, isError, error } = useBench(days);
  const [picked, setPicked] = useState<number | null>(null);
  if (isError) return <FetchError what="benchmarks" error={error} />;
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;
  if (data.runs.length === 0) return <Empty />;

  const run = data.runs.find((r) => r.id === picked) ?? data.runs[0];
  const config = Object.fromEntries(run.config.map((c) => [c.label, c]));
  const rows = run.variants.map((v) => [
    v.variant, v.correct, v.n, pct(v.accuracy), pct(v.low), pct(v.high),
    v.routed_n ? `${v.routed_ok}/${v.routed_n}` : "—", `${v.same}/${v.compared}`,
    v.errors, usd(v.avg_cost_usd), Math.round(v.avg_tokens)]);

  return (
    <div className="space-y-4 pt-4">
      {data.runs.length > 1 && (
        <div className="flex flex-wrap gap-1" role="group" aria-label="benchmark run">
          {data.runs.map((r) => (
            <button key={r.id} onClick={() => setPicked(r.id)} aria-pressed={r.id === run.id}
                    className={`rounded px-2 py-1 text-xs tabular-nums ${
                      r.id === run.id ? "bg-muted" : "text-muted-foreground"}`}>
              #{r.id} · {when(r.ts)}</button>
          ))}
        </div>
      )}

      <Card><CardContent className="flex flex-wrap items-baseline gap-x-4 gap-y-1 p-4 text-sm">
        <span className="font-mono">bench #{run.id}</span>
        <span>{run.suite}</span>
        <span className="tabular-nums">{run.cases} triage run{run.cases === 1 ? "" : "s"} ×{" "}
          {run.variants.length} variant{run.variants.length === 1 ? "" : "s"}</span>
        <span className="text-muted-foreground">{when(run.ts)}</span>
        <span className="text-muted-foreground"><Status run={run} /></span>
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">
          ${run.cost_usd.toFixed(2)} · {run.tokens.toLocaleString()} tok</span>
      </CardContent></Card>

      <ChartFrame title="Variants, baseline first"
        columns={["variant", "correct", "n", "accuracy", "95% low", "95% high", "routing",
                  "same as then", "failed", "avg cost", "avg tokens"]}
        rows={rows}>
        <VariantRows variants={run.variants} />
      </ChartFrame>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">
          Routing, agreement and models</CardTitle></CardHeader>
        <CardContent>
          <Table>
            <TableHeader><TableRow>
              <TableHead>variant</TableHead><TableHead>routing</TableHead>
              <TableHead>same as then</TableHead><TableHead>failed</TableHead>
              <TableHead>models that answered</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {run.variants.map((v) => (
                <TableRow key={v.variant} className="align-top tabular-nums">
                  <TableCell className="text-xs">
                    <div className="font-mono">{v.variant}</div>
                    {config[v.variant]?.prompt && config[v.variant].prompt !== "builtin" && (
                      <div className="break-all font-mono text-muted-foreground">
                        {config[v.variant].prompt}</div>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    {v.routed_n ? `${v.routed_ok}/${v.routed_n} · ${pct(v.routing)}` : "—"}</TableCell>
                  <TableCell className="text-xs">{v.same}/{v.compared}</TableCell>
                  <TableCell className="text-xs">{v.errors}</TableCell>
                  <TableCell className="font-mono text-xs">{v.models.join(", ") || "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Misses key={run.id} run={run} onOpen={onOpen} />

      <Command text={`deputy bench show ${run.id}`} />
      <p className="text-xs text-muted-foreground">
        History is the answer key, and only where it can say: a ticket that merged
        wanted execute and the repos that merged; a question a human answered was
        needed; a human_only a human reset was wrong, and one nobody reset in a week
        stood. Everything else is left out. A failed call is counted beside the
        accuracy, never in it, and the range is there because twenty triage runs do not
        make a percentage. Routing only counts triage runs that offered more than one
        repo. "Same as then" is agreement with what triage decided at the time, not
        a grade. Click a row to open its ticket.
      </p>
    </div>
  );
}
