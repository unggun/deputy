import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip,
         XAxis, YAxis } from "recharts";
import { useSpend } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { FetchError } from "@/components/FetchError";
import { GRID, MUTED, SERIES, STATIONS, SURFACE } from "@/lib/palette";
import { useColorMode } from "@/lib/useColorMode";

// Round once, here, so the stacked bar, its hover tooltip and the table view
// all show the same cents -- not a rounded table cell next to a raw-float
// tooltip.
const round2 = (v: number) => Math.round(v * 100) / 100;

/** One chart row: the day label plus one numeric field per station kind. */
type SpendRow = Record<string, string | number>;

// The judges' runs (ledger kind `score`) and the improve station's
// (`improve`), stacked on top as one band in the neutral tone: overhead on the
// work, not a sixth station, so it takes no series slot. One band, not two,
// because two bands in the same neutral tone could not be told apart.
const OVERHEAD: Record<string, string> = { score: "scoring", improve: "improving" };
const OVER = "overhead";

export function Spend({ days = 30 }: { days?: number }) {
  const { data, isError, error } = useSpend(days);
  const m = useColorMode();
  // React Query leaves `data` undefined once its retries are exhausted, so
  // without this branch a dead API spins on "loading…" forever.
  if (isError) return <FetchError what="spend" error={error} />;
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;

  // One row per day, one numeric field per station kind. Days with no work are
  // absent rather than zero-filled -- an absent bar reads as "nothing ran",
  // which is the truth.
  const rows: SpendRow[] = data.days.map((d) => {
    const out: SpendRow = { day: d.day.slice(5) };
    for (const s of STATIONS) {
      out[`${s}_tok`] = d.kinds?.[s]?.tokens ?? 0;
      out[`${s}_usd`] = round2(d.kinds?.[s]?.cost_usd ?? 0);
    }
    const over = Object.keys(OVERHEAD).map((k) => d.kinds?.[k]);
    out[`${OVER}_tok`] = over.reduce((a, k) => a + (k?.tokens ?? 0), 0);
    out[`${OVER}_usd`] = round2(over.reduce((a, k) => a + (k?.cost_usd ?? 0), 0));
    return out;
  });

  // Overhead only joins the stack once a judge or an improve run has spent
  // something, so a setup with neither keeps the five-station legend it has
  // always had; the band's name says which of the two it holds.
  const seen = Object.keys(OVERHEAD).filter((k) => data.days.some((d) => d.kinds?.[k]));
  const overName = seen.map((k) => OVERHEAD[k]).join(" + ");
  const kinds: readonly string[] = seen.length ? [...STATIONS, OVER] : STATIONS;
  const label = (k: string) => (k === OVER ? overName : k);
  const bars = (suffix: "tok" | "usd") =>
    kinds.map((s, i) => (
      <Bar key={s} dataKey={`${s}_${suffix}`} name={label(s)} stackId="a"
           fill={s === OVER ? MUTED : SERIES[m][i]}
           // 2px surface gap between stacked segments.
           stroke={SURFACE[m]} strokeWidth={2}
           radius={i === kinds.length - 1 ? [4, 4, 0, 0] : 0} />
    ));

  const axis = { stroke: MUTED, fontSize: 11, tickLine: false, axisLine: false };

  return (
    <div className="space-y-4 pt-4">
      {/* Tokens and dollars are different scales. Two charts, one x-range --
          never one chart with two y-axes. */}
      <ChartFrame title="Tokens per day, by station"
        columns={["day", ...kinds.map(label)]}
        rows={rows.map((r) => [r.day, ...kinds.map((s) => r[`${s}_tok`])])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={rows}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={56} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" labelStyle={{ color: MUTED }} />
            {bars("tok")}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      <ChartFrame title="Spend per day, by station"
        columns={["day", ...kinds.map(label)]}
        rows={rows.map((r) => [r.day,
                               ...kinds.map((s) => Number(r[`${s}_usd`]).toFixed(2))])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={rows}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={56} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" labelStyle={{ color: MUTED }} />
            {bars("usd")}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      {/* A ranking, not a composition: the spec's "Other" band would be
          meaningless here because the bars do not sum to a whole. Top 10 by
          tokens, and the table view carries the exact figures. */}
      <ChartFrame title="Most expensive rows"
        columns={["ticket", "repo", "tokens", "usd"]}
        rows={data.by_task.map((t) =>
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
