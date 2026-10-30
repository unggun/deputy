import { useImprovements, type Improvement, type ImprovementEvidence,
         type ImprovementsData } from "@/lib/api";
import { Command } from "@/components/Command";
import { FetchError } from "@/components/FetchError";
import { StatTile } from "@/components/StatTile";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";
import { STATUS } from "@/lib/palette";
import { pct, when } from "@/lib/format";

type Open = (id: string, station?: string) => void;

function Evidence({ e, onOpen }: { e: ImprovementEvidence; onOpen: Open }) {
  const what = e.kind === "score" ? `${e.scorer}: ${e.label}` : `review: ${e.label}`;
  if (!e.open_id) {
    return (
      <span className="rounded border px-2 py-0.5 font-mono text-xs text-muted-foreground"
            title="this verdict or run is no longer recorded">{e.ref}</span>
    );
  }
  return (
    <button onClick={() => onOpen(e.open_id!, e.log ?? undefined)}
            title={e.title ?? undefined}
            className="rounded border px-2 py-0.5 text-left font-mono text-xs hover:bg-muted">
      {e.task_key} <span className="text-muted-foreground">{e.log}</span> · {what}
    </button>
  );
}

function Proposal({ p, onOpen }: { p: Improvement; onOpen: Open }) {
  return (
    <Card>
      <CardContent className="space-y-3 p-4 text-sm">
        <div className="flex flex-wrap items-baseline gap-x-3 text-xs text-muted-foreground">
          <span className="font-mono text-foreground">#{p.id}</span>
          <span className="font-mono">{p.source}/{p.repo_key}</span>
          <span>{when(p.ts)}</span>
          <span className="tabular-nums">cites {p.evidence.length} of {p.shown} shown</span>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            note for the brain</div>
          <p className="mt-1 rounded border-l-2 pl-3 font-medium">{p.note}</p>
        </div>
        {p.problem && (
          <div><div className="text-xs text-muted-foreground">what keeps going wrong</div>
            <p>{p.problem}</p></div>
        )}
        {p.solution && (
          <div><div className="text-xs text-muted-foreground">why this note fixes it</div>
            <p>{p.solution}</p></div>
        )}
        <div className="flex flex-wrap gap-1">
          {p.evidence.map((e) => <Evidence key={e.ref} e={e} onOpen={onOpen} />)}
        </div>
        {p.reasoning && (
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">what it checked</summary>
            <p className="mt-1 whitespace-pre-wrap">{p.reasoning}</p>
          </details>
        )}
        <div className="space-y-1">
          <Command text={`deputy improve accept ${p.id}`} />
          <Command text={`deputy improve reject ${p.id} --reason "..."`} />
        </div>
      </CardContent>
    </Card>
  );
}

function Status({ s }: { s: Improvement["status"] }) {
  // The word always; the icon and its hue are a second channel.
  if (s === "accepted") return <span><span aria-hidden style={{ color: STATUS.good }}>✓ </span>accepted</span>;
  if (s === "rejected") return <span><span aria-hidden style={{ color: STATUS.critical }}>✕ </span>rejected</span>;
  if (s === "error") return <span><span aria-hidden style={{ color: STATUS.warning }}>⚠ </span>error</span>;
  if (s === "running") return <span className="text-muted-foreground">running…</span>;
  return <span className="text-muted-foreground">no proposal</span>;
}

function Empty({ data }: { data: ImprovementsData }) {
  const s = data.settings;
  return (
    <Card className="mt-4"><CardContent className="space-y-2 p-6 text-sm text-muted-foreground">
      {s.enabled ? (
        <p>The improve station is on, but nothing has been proposed in this window. A
          repo is looked at once it has {s.min_evidence} failures no earlier run has
          seen, or one that is {s.max_age_days} days old. Failures are failed scorer
          verdicts and reviews that asked for changes.</p>
      ) : (
        <>
          <p>The improve station is off, so nothing is proposed and nothing is spent.</p>
          <p>It reads a repo's failed scorer verdicts and blocking reviews and drafts
            one brain note that would have prevented them, for you to accept or
            reject. Try it on one repo with{" "}
            <span className="font-mono">deputy improve run --repo KEY</span>, and turn it
            on with <span className="font-mono">runner.improve.enabled: true</span>.</p>
        </>
      )}
    </CardContent></Card>
  );
}

export function Improvements({ days, onOpen }: { days: number; onOpen: Open }) {
  const { data, isError, error } = useImprovements(days);
  if (isError) return <FetchError what="improvements" error={error} />;
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;
  if (data.items.length === 0) return <Empty data={data} />;

  const pending = data.items.filter((i) => i.status === "pending");
  const decided = data.items.filter((i) => i.status === "accepted" || i.status === "rejected");
  const quiet = data.items.filter((i) => !["pending", "accepted", "rejected"].includes(i.status));
  const range = data.low == null || data.high == null ? null
    : `${pct(data.low)}–${pct(data.high)}`;

  return (
    <div className="space-y-4 pt-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="accepted" value={pct(data.rate)}
                  sub={range ? `95% range ${range} · n=${data.n}` : "nothing decided yet"} />
        <StatTile label="waiting for you" value={String(data.pending)}
                  sub={data.pending ? "accept or reject at the terminal" : undefined} />
        <StatTile label="no proposal" value={String(data.none)}
                  sub={data.errors ? `${data.errors} failed run${data.errors === 1 ? "" : "s"}`
                                   : undefined} />
        <StatTile label="cost" value={`$${data.cost_usd.toFixed(2)}`}
                  sub={`${data.tokens.toLocaleString()} tok`} />
      </div>
      {!data.settings.enabled && (
        <p className="text-xs text-muted-foreground">
          The station is off in config; these came from{" "}
          <span className="font-mono">deputy improve run</span> or from before it was
          turned off.</p>
      )}

      <section className="space-y-2">
        <h2 className="text-base font-semibold">Needs review</h2>
        {pending.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing is waiting for you.</p>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {pending.map((p) => <Proposal key={p.id} p={p} onOpen={onOpen} />)}
          </div>
        )}
      </section>

      {decided.length > 0 && (
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Decided</CardTitle></CardHeader>
          <CardContent>
            <Table>
              <TableHeader><TableRow>
                <TableHead>when</TableHead><TableHead>repo</TableHead>
                <TableHead>decision</TableHead><TableHead>note</TableHead>
                <TableHead>reason given</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {decided.map((p) => (
                  <TableRow key={p.id} className="align-top">
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {when(p.decided_at ?? p.ts)}</TableCell>
                    <TableCell className="font-mono text-xs">{p.repo_key}</TableCell>
                    <TableCell className="whitespace-nowrap text-xs"><Status s={p.status} /></TableCell>
                    <TableCell className="min-w-64 whitespace-normal text-xs">{p.note}</TableCell>
                    <TableCell className="min-w-48 whitespace-normal text-xs text-muted-foreground">
                      {p.reason ?? ""}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {quiet.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Runs that proposed nothing</CardTitle></CardHeader>
          <CardContent>
            <Table>
              <TableHeader><TableRow>
                <TableHead>when</TableHead><TableHead>repo</TableHead>
                <TableHead>outcome</TableHead><TableHead>shown</TableHead>
                <TableHead>why</TableHead><TableHead>cost</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {quiet.map((p) => (
                  <TableRow key={p.id} className="align-top tabular-nums">
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {when(p.ts)}</TableCell>
                    <TableCell className="font-mono text-xs">{p.repo_key}</TableCell>
                    <TableCell className="whitespace-nowrap text-xs"><Status s={p.status} /></TableCell>
                    <TableCell className="text-xs">{p.shown}</TableCell>
                    <TableCell className="min-w-64 whitespace-normal text-xs">
                      {p.reasoning ?? ""}</TableCell>
                    <TableCell className="text-xs">${p.cost_usd.toFixed(3)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <p className="text-xs text-muted-foreground">
        Each run reads one repo's failures that no earlier run has seen and proposes
        at most one brain note, citing the failures it addresses. Nothing reaches the
        brain until you accept it at the terminal. A rejection and its reason are shown
        to every later run on the repo. The rate is accepted over decided; pending
        proposals are listed whatever the date range. Click a piece of evidence to read
        that run's log.
      </p>
    </div>
  );
}
