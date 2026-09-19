import { useEffect, useMemo, useState } from "react";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { StateBadge } from "@/components/StateBadge";
import { useTask, useTaskLog } from "@/lib/api";

function LogTail({ id, station }: { id: string; station: string }) {
  // Poll forward from the offset the last read returned rather than
  // re-fetching the file. A killed run still left evidence on disk, so a
  // finished station reads the same way a running one does.
  //
  // `useTaskLog`'s query key includes `offset`, and it refetches on a 2s
  // interval. That means the SAME offset can legitimately be re-fetched more
  // than once before it advances -- a partial record at the end of the file
  // makes `tail()` return `next_offset === offset` with zero (or, once the
  // record completes, some) lines, and StrictMode's dev double-invoke can
  // redeliver an identical payload for a key that hasn't changed. Accumulating
  // with `setLines(prev => [...prev, ...data.lines])` keyed only on `[data]`
  // would append that batch again on every such redelivery.
  //
  // Fix: key the accumulator by the offset the batch STARTED from, so a
  // repeat delivery for that offset overwrites the same slot instead of
  // appending a new one. This also does the right thing for the legitimate
  // case above -- a slot that was empty while a record was mid-write gets
  // overwritten with real content once it completes, never duplicated.
  const [offset, setOffset] = useState(0);
  const [chunks, setChunks] = useState<Record<number, any[]>>({});
  const { data } = useTaskLog(id, station, offset);

  // No reset-on-[id, station] effect here: `TaskSheet` mounts this component
  // with `key={`${id}:${station}`}`, so a row or station switch remounts it
  // from scratch (fresh `offset`/`chunks`) rather than updating it in place --
  // those deps can never change within one instance's lifetime.

  useEffect(() => {
    if (!data || data.missing) return;
    if (data.lines.length > 0) {
      setChunks((prev) => ({ ...prev, [offset]: data.lines }));
    }
    if (data.next_offset !== offset) setOffset(data.next_offset);
    // `offset` is intentionally a dependency: `data` alone does not tell us
    // which offset it was fetched from, and that offset is the overwrite key.
  }, [data, offset]);

  const lines = useMemo(
    () => Object.keys(chunks)
      .map(Number)
      .sort((a, b) => a - b)
      .flatMap((k) => chunks[k]),
    [chunks],
  );

  if (data?.missing) {
    return <div className="p-4 text-sm text-muted-foreground">
      This station has not run for this row.</div>;
  }
  return (
    <ScrollArea className="h-[60vh] rounded border">
      <pre className="p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
        {lines.map((l, i) => (
          <div key={i}>
            <span className="text-muted-foreground">{l.type}</span>{" "}
            {l.text ?? JSON.stringify(l)}
          </div>
        ))}
      </pre>
    </ScrollArea>
  );
}

export function TaskSheet({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { data: row } = useTask(id);
  const [station, setStation] = useState<string | null>(null);
  useEffect(() => { setStation(row?.stations?.[0] ?? null); }, [row?.task_id]);

  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-3xl">
        {row && (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2 font-mono text-sm">
                {row.task_key}<StateBadge state={row.state} />
                <span className="text-muted-foreground">{row.repo_key}</span>
              </SheetTitle>
            </SheetHeader>
            <div className="mt-2 text-sm">{row.title}</div>
            <div className="mt-1 flex flex-wrap gap-3 text-xs text-muted-foreground">
              {row.branch && <span className="font-mono">{row.branch}</span>}
              {row.pr_url && <a className="underline" href={row.pr_url}
                                target="_blank" rel="noreferrer">PR #{row.pr_number}</a>}
              <span>{row.attempts} attempts</span>
              <span>{row.tokens.toLocaleString()} tok · ${row.cost_usd.toFixed(2)}</span>
            </div>

            {/* Keyed on `id`: the Sheet's `open` prop never passes through
                `false` when the row switches (Board/Overview hand `onOpen` a
                new id directly), so Dialog.Content never unmounts and this
                uncontrolled Tabs would otherwise keep whatever value was
                selected on the PREVIOUS row. If that value has no matching
                trigger/content on the new row (e.g. "error" when the new row
                has no last_error), nothing renders as active -- a blank pane
                instead of falling back to Timeline. Keying on `id` forces a
                remount on every row switch, same as `LogTail` already does. */}
            <Tabs key={id ?? undefined} defaultValue="timeline" className="mt-4">
              <TabsList>
                <TabsTrigger value="timeline">Timeline</TabsTrigger>
                <TabsTrigger value="logs">Logs</TabsTrigger>
                {row.last_error && <TabsTrigger value="error">Error</TabsTrigger>}
              </TabsList>

              <TabsContent value="timeline">
                <div className="divide-y text-sm">
                  {(row.events ?? []).map((e, i) => (
                    <div key={i} className="flex gap-3 py-1.5">
                      <span className="w-32 shrink-0 text-xs text-muted-foreground tabular-nums">
                        {new Date(e.ts * 1000).toLocaleString()}</span>
                      <span className="w-20 shrink-0 font-mono text-xs">{e.kind}</span>
                      <span className="min-w-0 flex-1 truncate">{e.detail}</span>
                      {e.tokens > 0 && (
                        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                          {e.tokens.toLocaleString()} tok</span>)}
                    </div>
                  ))}
                </div>
              </TabsContent>

              <TabsContent value="logs">
                <div className="mb-2 flex gap-1">
                  {(row.stations ?? []).map((s) => (
                    <button key={s} onClick={() => setStation(s)}
                            className={`rounded px-2 py-1 font-mono text-xs ${
                              s === station ? "bg-muted" : "text-muted-foreground"}`}>
                      {s}</button>
                  ))}
                </div>
                {id && station && <LogTail key={`${id}:${station}`} id={id} station={station} />}
                {(row.stations ?? []).length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">
                    No agent has run for this row yet.</div>)}
              </TabsContent>

              {row.last_error && (
                <TabsContent value="error">
                  <pre className="whitespace-pre-wrap rounded border p-3 font-mono text-xs">
                    {row.last_error}</pre>
                </TabsContent>
              )}
            </Tabs>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
