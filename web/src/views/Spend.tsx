import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip,
         XAxis, YAxis } from "recharts";
import { useSpend } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { GRID, MUTED, SERIES, STATIONS, SURFACE } from "@/lib/palette";
import { useColorMode } from "@/lib/useColorMode";

export function Spend() {
  const { data } = useSpend(30);
  const m = useColorMode();
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;

  // One row per day, one numeric field per station kind. Days with no work are
  // absent rather than zero-filled -- an absent bar reads as "nothing ran",
  // which is the truth.
  const rows = data.days.map((d: any) => {
    const out: any = { day: d.day.slice(5) };
    for (const s of STATIONS) {
      out[`${s}_tok`] = d.kinds?.[s]?.tokens ?? 0;
      out[`${s}_usd`] = d.kinds?.[s]?.cost_usd ?? 0;
    }
    return out;
  });

  const bars = (suffix: "tok" | "usd") =>
    STATIONS.map((s, i) => (
      <Bar key={s} dataKey={`${s}_${suffix}`} name={s} stackId="a"
           fill={SERIES[m][i]}
           // 2px surface gap between stacked segments.
           stroke={SURFACE[m]} strokeWidth={2}
           radius={i === STATIONS.length - 1 ? [4, 4, 0, 0] : 0} />
    ));

  const axis = { stroke: MUTED, fontSize: 11, tickLine: false, axisLine: false };

  return (
    <div className="space-y-4 pt-4">
      {/* Tokens and dollars are different scales. Two charts, one x-range --
          never one chart with two y-axes. */}
      <ChartFrame title="Tokens per day, by station"
        columns={["day", ...STATIONS]}
        rows={rows.map((r: any) => [r.day, ...STATIONS.map((s) => r[`${s}_tok`])])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={rows}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={56} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" />
            {bars("tok")}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      <ChartFrame title="Spend per day, by station"
        columns={["day", ...STATIONS]}
        rows={rows.map((r: any) => [r.day, ...STATIONS.map((s) => r[`${s}_usd`].toFixed(2))])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={rows}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={56} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" />
            {bars("usd")}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      {/* A ranking, not a composition: the spec's "Other" band would be
          meaningless here because the bars do not sum to a whole. Top 10 by
          tokens, and the table view carries the exact figures. */}
      <ChartFrame title="Most expensive rows"
        columns={["ticket", "repo", "tokens", "usd"]}
        rows={data.by_task.map((t: any) =>
          [t.task_key, t.repo_key ?? "", t.tokens.toLocaleString(), t.cost_usd.toFixed(2)])}>
        <ResponsiveContainer width="100%" height={Math.max(160, data.by_task.length * 28)}>
          <BarChart data={data.by_task} layout="vertical">
            <CartesianGrid stroke={GRID[m]} horizontal={false} />
            <XAxis type="number" {...axis} />
            <YAxis type="category" dataKey="task_key" {...axis} width={90} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Bar dataKey="tokens" fill={SERIES[m][0]} radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </div>
  );
}
