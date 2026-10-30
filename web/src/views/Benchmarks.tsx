import { useState, type KeyboardEvent, type ReactNode } from "react";
import { useBench, type BenchMiss, type BenchRun, type ImplBenchRun, type ImplMiss,
         type TriageBenchRun } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { Command } from "@/components/Command";
import { FetchError } from "@/components/FetchError";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";
import { GRID, SERIES, STATUS, SURFACE } from "@/lib/palette";
import { useColorMode } from "@/lib/useColorMode";
import { duration, pct, when } from "@/lib/format";

type Open = (id: string, station?: string) => void;

const usd = (v: number) => `$${v.toFixed(4)}`;
const many = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

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

/** What one variant row draws: a rate with its 95% range, and a cost. */
type Track = {
  variant: string; rate: number | null; low: number | null; high: number | null;
  said: string; cost: number; costText: string;
};

/** One variant per row: a rate with its 95% range on one axis, cost on
 *  another. Two measures, two scales, so two tracks -- never one chart with
 *  two y-axes. Plain HTML rather than a chart library, so a long variant
 *  name wraps above its tracks on a phone instead of being cut. */
function VariantRows({ rows, rateHead, costHead }: {
  rows: Track[]; rateHead: string; costHead: string;
}) {
  const m = useColorMode();
  const hue = SERIES[m][0];
  const maxCost = Math.max(...rows.map((v) => v.cost), 0);
  const tick = (left: number) => (
    <div className="absolute inset-y-0 w-px" style={{ left: `${left}%`, background: GRID[m] }} />
  );
  return (
    <div className="space-y-3">
      <div className="hidden grid-cols-[minmax(0,14rem)_1fr_10rem] gap-4 text-xs
                      text-muted-foreground md:grid">
        <span>variant</span>
        <span className="flex justify-between"><span>0%</span>
          <span>{rateHead}, with 95% range</span><span>100%</span></span>
        <span>{costHead}</span>
      </div>
      <p className="text-xs text-muted-foreground md:hidden">
        Each variant: {rateHead} on a 0–100% track, with its 95% range, then
        its {costHead}.</p>
      {rows.map((v) => (
        <div key={v.variant} title={`${v.variant}\n${v.said}\n${v.costText}`}
             className="grid gap-x-4 gap-y-1 md:grid-cols-[minmax(0,14rem)_1fr_10rem]
                        md:items-center">
          <div className="min-w-0 break-words font-mono text-xs">{short(v.variant)}</div>
          <div>
            <div className="relative h-6" role="img"
                 aria-label={`${short(v.variant)}: ${rateHead} ${v.said}`}>
              {[0, 25, 50, 75, 100].map((t) => <span key={t}>{tick(t)}</span>)}
              {v.low != null && v.high != null && (
                <div className="absolute top-1/2 h-0.5 -translate-y-1/2"
                     style={{ left: `${v.low * 100}%`,
                              width: `${(v.high - v.low) * 100}%`, background: hue }} />
              )}
              {v.rate != null && (
                <div className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2
                                rounded-full"
                     style={{ left: `${v.rate * 100}%`, background: hue,
                              boxShadow: `0 0 0 2px ${SURFACE[m]}` }} />
              )}
            </div>
            <div className="text-xs tabular-nums text-muted-foreground">{v.said}</div>
          </div>
          <div>
            <div className="relative h-6">
              <div className="absolute inset-y-1.5 left-0 rounded-r"
                   style={{ width: maxCost ? `${(v.cost / maxCost) * 100}%` : 0,
                            minWidth: v.cost ? 2 : 0, background: hue }} />
            </div>
            <div className="text-xs tabular-nums text-muted-foreground">{v.costText}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** "3/5 · 60% (23%–88%)", or what stands in for a rate nobody can give. */
function rated(k: number, n: number, rate: number | null, low: number | null,
               high: number | null, none: string) {
  if (rate == null) return none;
  const range = low == null || high == null ? "" : ` (${pct(low)}–${pct(high)})`;
  return `${k}/${n} · ${pct(rate)}${range}`;
}

/** A row that opens its ticket, by mouse or keyboard, when it has one. */
function opener(id: string | null, onOpen: Open) {
  if (!id) return {};
  return {
    role: "button", tabIndex: 0, onClick: () => onOpen(id),
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault(); onOpen(id);
      }
    },
  };
}

/** Filter buttons over the variants a list of misses mentions. */
function VariantFilter({ names, value, onChange }: {
  names: string[]; value: string | null; onChange: (v: string | null) => void;
}) {
  if (names.length < 2) return null;
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label="variant">
      {[null, ...names].map((n) => (
        <button key={n ?? "all"} onClick={() => onChange(n)} aria-pressed={value === n}
                className={`rounded px-2 py-1 font-mono text-xs ${
                  value === n ? "bg-muted" : "text-muted-foreground"}`}>
          {n ? short(n) : "all"}</button>
      ))}
    </div>
  );
}

function MissCard<T extends { id: number; variant: string }>({ title, misses, empty, head, row }: {
  title: string; misses: T[]; empty: string; head: string[]; row: (x: T) => ReactNode;
}) {
  const [variant, setVariant] = useState<string | null>(null);
  const names = [...new Set(misses.map((x) => x.variant))];
  const shown = misses.filter((x) => !variant || x.variant === variant);
  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center gap-2 pb-2">
        <CardTitle className="mr-auto text-base">{title}</CardTitle>
        <VariantFilter names={names} value={variant} onChange={setVariant} />
      </CardHeader>
      <CardContent>
        {shown.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">{empty}</p>
        ) : (
          <Table>
            <TableHeader><TableRow>
              {head.map((h) => <TableHead key={h}>{h}</TableHead>)}
            </TableRow></TableHeader>
            <TableBody>{shown.map(row)}</TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function Ticket({ k, title, sub }: { k: string; title: string | null; sub?: string | null }) {
  return (
    <TableCell className="max-w-48">
      <div className="font-mono text-xs">{k}</div>
      {title && <div className="truncate text-xs">{title}</div>}
      {sub && <div className="font-mono text-xs text-muted-foreground">{sub}</div>}
    </TableCell>
  );
}

const Wrong = () => <span aria-hidden style={{ color: STATUS.critical }}>✕ </span>;

// --- triage ----------------------------------------------------------------

/** A decision as the pipeline would act on it: repos only matter to execute,
 *  and `_pick_repos` fills them in for the other two as well. */
function said(decision: string | null, repos: string[]) {
  if (decision == null) return "failed";
  return decision === "execute" && repos.length ? `execute → ${repos.join(", ")}` : decision;
}

function TriageRun({ run, onOpen }: { run: TriageBenchRun; onOpen: Open }) {
  const config = Object.fromEntries(run.config.map((c) => [c.label, c]));
  const rows = run.variants.map((v) => [
    v.variant, v.correct, v.n, pct(v.accuracy), pct(v.low), pct(v.high),
    v.routed_n ? `${v.routed_ok}/${v.routed_n}` : "—", `${v.same}/${v.compared}`,
    v.errors, usd(v.avg_cost_usd), Math.round(v.avg_tokens)]);
  const tracks = run.variants.map((v) => ({
    variant: v.variant, rate: v.accuracy, low: v.low, high: v.high,
    said: rated(v.correct, v.n, v.accuracy, v.low, v.high, "no graded calls")
      + (v.errors > 0 ? ` · ${v.errors} failed` : ""),
    cost: v.avg_cost_usd,
    costText: `${usd(v.avg_cost_usd)} · ${Math.round(v.avg_tokens).toLocaleString()} tok`,
  }));
  return (
    <>
      <ChartFrame title="Variants, baseline first"
        columns={["variant", "correct", "n", "accuracy", "95% low", "95% high", "routing",
                  "same as then", "failed", "avg cost", "avg tokens"]}
        rows={rows}>
        <VariantRows rows={tracks} rateHead="accuracy against history"
                     costHead="average cost per call" />
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

      <MissCard<BenchMiss> key={run.id} title="Where a variant disagreed with history"
        misses={run.misses} empty="Every graded call agreed with history."
        head={["ticket", "history", "variant said", "its reasoning"]}
        row={(x) => (
          <TableRow key={x.id} {...opener(x.open_id, onOpen)}
                    className={`align-top ${x.open_id ? "cursor-pointer" : ""}`}>
            <Ticket k={x.task_key ?? x.case} title={x.title} />
            <TableCell className="whitespace-nowrap text-xs">
              <div>{x.correct === 1 && x.routed === 0
                ? said("execute", x.expected_repos ?? [])
                : x.expected.join(" or ")}</div>
              <div className="text-muted-foreground">{x.why}
                {x.recorded && ` · said ${x.recorded} then`}</div>
            </TableCell>
            <TableCell className="text-xs">
              <div className="whitespace-nowrap"><Wrong />{said(x.decision, x.repos)}</div>
              <div className="font-mono text-muted-foreground">{short(x.variant)}</div>
            </TableCell>
            <TableCell className="min-w-64 whitespace-normal text-xs">
              {x.reasoning ?? ""}</TableCell>
          </TableRow>
        )} />

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
    </>
  );
}

// --- implement -------------------------------------------------------------

const tested = (t: number | null) =>
  t === 1 ? "tests passed" : t === 0 ? "tests failed" : "tests not run";

function ImplementRun({ run, onOpen }: { run: ImplBenchRun; onOpen: Open }) {
  const left = run.variants.reduce((a, v) => a + v.left_out, 0);
  const rows = run.variants.map((v) => [
    v.variant, v.correct, v.n, pct(v.rate), pct(v.low), pct(v.high),
    v.tests_n ? `${v.tests_ok}/${v.tests_n}` : "—", v.partial, v.incorrect, v.no_changes,
    v.insufficient, v.errors, `$${v.avg_cost_usd.toFixed(2)}`, v.avg_minutes.toFixed(1)]);
  const tracks = run.variants.map((v) => ({
    variant: v.variant, rate: v.rate, low: v.low, high: v.high,
    said: rated(v.correct, v.n, v.rate, v.low, v.high, "no graded runs")
      + (v.errors > 0 ? ` · ${v.errors} failed` : ""),
    cost: v.avg_cost_usd,
    costText: `$${v.avg_cost_usd.toFixed(2)} · ${duration(v.avg_minutes * 60)}`,
  }));
  return (
    <>
      <ChartFrame title="Variants, baseline first"
        columns={["variant", "correct", "n", "rate", "95% low", "95% high", "tests passed",
                  "partial", "incorrect", "no changes", "judge unsure", "failed",
                  "avg cost", "avg minutes"]}
        rows={rows}>
        <VariantRows rows={tracks} rateHead="correct, by the judge and the tests"
                     costHead="average cost per run" />
      </ChartFrame>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">
          Tests, verdicts and time</CardTitle></CardHeader>
        <CardContent>
          <Table>
            <TableHeader><TableRow>
              <TableHead>variant</TableHead><TableHead>tests passed</TableHead>
              <TableHead>partial</TableHead><TableHead>incorrect</TableHead>
              <TableHead>no changes</TableHead><TableHead>judge unsure</TableHead>
              <TableHead>failed</TableHead><TableHead>avg time</TableHead>
              <TableHead>models that answered</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {run.variants.map((v) => (
                <TableRow key={v.variant} className="align-top tabular-nums">
                  <TableCell className="font-mono text-xs">{v.variant}</TableCell>
                  <TableCell className="text-xs">
                    {v.tests_n ? `${v.tests_ok}/${v.tests_n} · ${pct(v.tests_rate)}` : "—"}
                  </TableCell>
                  <TableCell className="text-xs">{v.partial}</TableCell>
                  <TableCell className="text-xs">{v.incorrect}</TableCell>
                  <TableCell className="text-xs">{v.no_changes}</TableCell>
                  <TableCell className="text-xs">{v.insufficient}</TableCell>
                  <TableCell className="text-xs">{v.errors}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs">
                    {duration(v.avg_minutes * 60)}</TableCell>
                  <TableCell className="font-mono text-xs">{v.models.join(", ") || "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {left > 0 && (
            <p className="pt-3 text-xs text-muted-foreground">
              {many(left, "run")} on a ticket not every variant finished
              {left === 1 ? " is" : " are"} left out of every number here.</p>
          )}
        </CardContent>
      </Card>

      <MissCard<ImplMiss> key={run.id} title="Runs graded wrong" misses={run.misses}
        empty="Every graded run was correct."
        head={["ticket", "variant", "graded", "diff", "the judge's reasoning"]}
        row={(x) => (
          <TableRow key={x.id} {...opener(x.open_id, onOpen)}
                    className={`align-top ${x.open_id ? "cursor-pointer" : ""}`}>
            <Ticket k={x.task_key ?? x.case} title={x.title} sub={x.repo_key} />
            <TableCell className="font-mono text-xs">{short(x.variant)}</TableCell>
            <TableCell className="whitespace-nowrap text-xs">
              <div><Wrong />{x.decision ?? "no verdict"}</div>
              <div className="text-muted-foreground">{tested(x.tests)}</div>
            </TableCell>
            <TableCell className="whitespace-nowrap text-xs tabular-nums">
              {x.files == null ? "—"
                : `${many(x.files, "file")} · +${x.additions ?? 0} −${x.deletions ?? 0}`}
            </TableCell>
            <TableCell className="min-w-64 whitespace-normal text-xs">
              {x.reasoning ?? ""}</TableCell>
          </TableRow>
        )} />

      <Command text={`deputy bench show ${run.id}`} />
      <p className="text-xs text-muted-foreground">
        Each run implements a ticket that merged, from the commit its branch started
        at, in a scratch copy with no remote and no later history, so nothing is
        pushed and the answer is not there to find. A run is correct
        when a judge with no tools, reading the ticket, the criteria and both diffs,
        says so, and the repo's tests pass. The tests only grade where the change that
        merged passes them on this machine too. A judge unsure about the diffs and a
        failed run are counted beside the rate, never in it. Click a row to open its
        ticket; the diffs and logs are under the runner's log directory.
      </p>
    </>
  );
}

function Empty() {
  return (
    <Card className="mt-4"><CardContent className="space-y-3 p-6 text-sm text-muted-foreground">
      <p>No benchmark runs in this window. A benchmark replays tickets the runner has
        already handled, under another model or prompt, and grades each one against
        what happened to the ticket afterwards. Nothing runs on its own: it is a
        command you run when you are weighing a change.</p>
      <p>See what history can grade, and what a run would cost, without calling anything:</p>
      <Command text="deputy bench triage --dry-run" />
      <Command text="deputy bench implement --dry-run" />
      <p>Then try a triage model, or a prompt you edited from the built-in one:</p>
      <Command text="deputy bench triage --model haiku" />
      <Command text="deputy bench prompt > careful.md" />
      <Command text="deputy bench triage --prompt careful.md" />
      <p>Or replay merged tickets under another implementer model:</p>
      <Command text="deputy bench implement --model opus --limit 5" />
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
  const unit = run.suite === "implement" ? "ticket" : "triage run";

  return (
    <div className="space-y-4 pt-4">
      {data.runs.length > 1 && (
        <div className="flex flex-wrap gap-1" role="group" aria-label="benchmark run">
          {data.runs.map((r) => (
            <button key={r.id} onClick={() => setPicked(r.id)} aria-pressed={r.id === run.id}
                    className={`rounded px-2 py-1 text-xs tabular-nums ${
                      r.id === run.id ? "bg-muted" : "text-muted-foreground"}`}>
              #{r.id} · {r.suite} · {when(r.ts)}</button>
          ))}
        </div>
      )}

      <Card><CardContent className="flex flex-wrap items-baseline gap-x-4 gap-y-1 p-4 text-sm">
        <span className="font-mono">bench #{run.id}</span>
        <span>{run.suite}</span>
        <span className="tabular-nums">{many(run.cases, unit)} ×{" "}
          {many(run.variants.length, "variant")}</span>
        <span className="text-muted-foreground">{when(run.ts)}</span>
        <span className="text-muted-foreground"><Status run={run} /></span>
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">
          ${run.cost_usd.toFixed(2)} · {run.tokens.toLocaleString()} tok</span>
      </CardContent></Card>

      {run.suite === "implement"
        ? <ImplementRun run={run} onOpen={onOpen} />
        : <TriageRun run={run} onOpen={onOpen} />}
    </div>
  );
}
