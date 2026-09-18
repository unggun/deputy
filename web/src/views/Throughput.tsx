import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip,
         XAxis, YAxis } from "recharts";
import { useThroughput } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { GRID, MUTED, STATUS, SURFACE } from "@/lib/palette";

const mode = () =>
  window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

/** Bucket cycle times into human-sized bands rather than raw seconds. */
function histogram(seconds: number[]): { band: string; n: number }[] {
  const bands = [
    ["< 1h", 3600], ["1-4h", 14400], ["4-12h", 43200],
    ["12-24h", 86400], ["1-3d", 259200], ["> 3d", Infinity],
  ] as const;
  return bands.map(([band, cap], i) => {
    const floor = i === 0 ? 0 : (bands[i - 1][1] as number);
    return { band, n: seconds.filter((s) => s > floor && s <= cap).length };
  });
}

export function Throughput() {
  const { data } = useThroughput(30);
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;
  const m = mode();
  const axis = { stroke: MUTED, fontSize: 11, tickLine: false, axisLine: false };
  const days = data.days.map((d: any) => ({ ...d, day: d.day.slice(5) }));
  const hist = histogram(data.cycle_times.map((c: any) => c.seconds));

  const cycles: Record<number, number> = {};
  for (const r of data.review_cycles) cycles[r.cycles] = (cycles[r.cycles] ?? 0) + 1;
  const cycleRows = Object.entries(cycles)
    .map(([k, n]) => ({ cycles: `${k}x`, n })).sort((a, b) => a.cycles.localeCompare(b.cycles));

  return (
    <div className="space-y-4 pt-4">
      {/* These are states, not series, so they wear the reserved status
          palette -- with names in the legend, never color alone. */}
      <ChartFrame title="Outcomes per day"
        columns={["day", "pr-open", "done", "blocked"]}
        rows={days.map((d: any) => [d.day, d.pr_open, d.done, d.blocked])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={days}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={40} allowDecimals={false} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" />
            <Bar dataKey="pr_open" name="pr-open" fill={STATUS.serious}
                 stroke={SURFACE[m]} strokeWidth={2}
                 radius={[4, 4, 0, 0]} />
            <Bar dataKey="done" name="done" fill={STATUS.good}
                 stroke={SURFACE[m]} strokeWidth={2}
                 radius={[4, 4, 0, 0]} />
            <Bar dataKey="blocked" name="blocked" fill={STATUS.critical}
                 stroke={SURFACE[m]} strokeWidth={2}
                 radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      <div className="grid gap-4 md:grid-cols-2">
        <ChartFrame title="Time from first touch to done"
          columns={["band", "rows"]} rows={hist.map((h) => [h.band, h.n])}>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={hist}>
              <CartesianGrid stroke={GRID[m]} vertical={false} />
              <XAxis dataKey="band" {...axis} />
              <YAxis {...axis} width={32} allowDecimals={false} />
              <Tooltip cursor={{ fill: "transparent" }} />
              <Bar dataKey="n" fill={STATUS.good} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>

        <ChartFrame title="Review cycles per row"
          columns={["cycles", "rows"]} rows={cycleRows.map((c) => [c.cycles, c.n])}>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={cycleRows}>
              <CartesianGrid stroke={GRID[m]} vertical={false} />
              <XAxis dataKey="cycles" {...axis} />
              <YAxis {...axis} width={32} allowDecimals={false} />
              <Tooltip cursor={{ fill: "transparent" }} />
              <Bar dataKey="n" fill={STATUS.warning} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>
      </div>
    </div>
  );
}
