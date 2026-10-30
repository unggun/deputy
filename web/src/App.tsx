import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Overview } from "@/views/Overview";
import { Benchmarks } from "@/views/Benchmarks";
import { Board } from "@/views/Board";
import { Flow } from "@/views/Flow";
import { Improvements } from "@/views/Improvements";
import { Runs } from "@/views/Runs";
import { Scorers } from "@/views/Scorers";
import { Spend } from "@/views/Spend";
import { Throughput } from "@/views/Throughput";
import { GraphTab } from "@/views/GraphTab";
import { TaskSheet } from "@/views/TaskSheet";

const qc = new QueryClient();

const RANGES = [7, 14, 30, 90] as const;
// Overview, Board and Graph show the present, not a window; the range picker
// says so by going inert there rather than appearing to filter them.
const RANGED = new Set(["flow", "runs", "scorers", "improvements", "benchmarks", "spend",
                        "throughput"]);

export default function App() {
  const [tab, setTab] = useState("overview");
  const [days, setDays] = useState<number>(30);
  // Which task's detail sheet is open, and optionally which run's log. Every
  // view hands it a row's task_id via onOpen; TaskSheet is mounted outside
  // <Tabs> below so it opens the same way from any view.
  const [open, setOpen] = useState<{ id: string; station?: string } | null>(null);
  const onOpen = (id: string, station?: string) => setOpen({ id, station });
  return (
    <QueryClientProvider client={qc}>
      <div className="min-h-screen bg-background text-foreground p-4 md:p-6">
        <Tabs value={tab} onValueChange={setTab}>
          <div className="flex flex-wrap items-center gap-3">
            {/* Ten tabs are wider than a phone: the list scrolls sideways
                instead of pushing the whole page past the viewport. */}
            <TabsList className="max-w-full justify-start overflow-x-auto">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="board">Board</TabsTrigger>
              <TabsTrigger value="flow">Flow</TabsTrigger>
              <TabsTrigger value="runs">Runs</TabsTrigger>
              <TabsTrigger value="scorers">Scorers</TabsTrigger>
              <TabsTrigger value="improvements">Improvements</TabsTrigger>
              <TabsTrigger value="benchmarks">Benchmarks</TabsTrigger>
              <TabsTrigger value="spend">Spend</TabsTrigger>
              <TabsTrigger value="throughput">Throughput</TabsTrigger>
              <TabsTrigger value="graph">Graph</TabsTrigger>
            </TabsList>
            <div role="group" aria-label="date range"
                 title={RANGED.has(tab) ? undefined : "This tab shows the present, not a date range"}
                 className={`flex gap-1 ${RANGED.has(tab) ? "" : "opacity-40"}`}>
              {RANGES.map((d) => (
                <button key={d} onClick={() => setDays(d)} disabled={!RANGED.has(tab)}
                        aria-pressed={days === d}
                        className={`rounded px-2 py-1 text-xs tabular-nums ${
                          days === d ? "bg-muted" : "text-muted-foreground"}`}>
                  {d}d</button>
              ))}
            </div>
          </div>
          <TabsContent value="overview"><Overview onOpen={onOpen} /></TabsContent>
          <TabsContent value="board"><Board onOpen={onOpen} /></TabsContent>
          <TabsContent value="flow"><Flow days={days} onOpen={onOpen} /></TabsContent>
          <TabsContent value="runs"><Runs days={days} onOpen={onOpen} /></TabsContent>
          <TabsContent value="scorers"><Scorers days={days} onOpen={onOpen} /></TabsContent>
          <TabsContent value="improvements">
            <Improvements days={days} onOpen={onOpen} /></TabsContent>
          <TabsContent value="benchmarks">
            <Benchmarks days={days} onOpen={onOpen} /></TabsContent>
          <TabsContent value="spend"><Spend days={days} /></TabsContent>
          <TabsContent value="throughput"><Throughput days={days} /></TabsContent>
          <TabsContent value="graph"><GraphTab /></TabsContent>
        </Tabs>
        <TaskSheet id={open?.id ?? null} station={open?.station}
                   onClose={() => setOpen(null)} />
      </div>
    </QueryClientProvider>
  );
}
