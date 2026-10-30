import type { EventRow } from "@/lib/api";
import { MUTED, SERIES, STATE_STATUS, STATIONS, STATUS, SURFACE } from "@/lib/palette";
import { useColorMode } from "@/lib/useColorMode";
import { duration, runFailed, when } from "@/lib/format";

const MARKERS: Record<string, [string, string]> = {
  pr_ready: ["▸", "marked ready for review"],
  pr_review: ["●", "first human review"],
  pr_merged: ["✓", "merged"],
};

const isStation = (kind: string) => (STATIONS as readonly string[]).includes(kind);

/** A row's history as lanes on one time axis: the state it was in, the PR
 *  milestones, and one lane per station with a bar per agent run.
 *
 * A run's ledger line is written when it ends, so a bar spans
 * `ts - duration_ms` to `ts`. Runs are minutes and cycles are days, so most
 * bars are drawn at a minimum width: the lane says *when* the agent worked,
 * and the state lane says where the rest of the time went. Every mark has a
 * hover title, and the event list under it is the same history as text --
 * colour is never the only channel.
 */
export function Swimlanes({ events, live, now }: {
  events: EventRow[]; live: boolean; now: number;
}) {
  const m = useColorMode();
  if (events.length < 2) return null;
  const runs = events.filter((e) => isStation(e.kind));
  const startOf = (e: EventRow) => e.ts - (e.duration_ms ?? 0) / 1000;
  const t0 = Math.min(events[0].ts, ...runs.map(startOf));
  const last = events[events.length - 1].ts;
  // A live row runs to `now`, the time its data was fetched -- the moment
  // this picture is true of.
  const t1 = live ? Math.max(last, now) : last;
  const span = t1 - t0;
  if (span <= 0) return null;
  const x = (t: number) => `${((t - t0) / span) * 100}%`;
  const w = (a: number, b: number) => `${((b - a) / span) * 100}%`;

  const states = events.filter((e) => e.kind === "state");
  const segments = states.map((e, i) => ({
    state: String(e.detail ?? "").split("->").pop() ?? "",
    start: e.ts,
    end: i + 1 < states.length ? states[i + 1].ts : t1,
  })).filter((s) => s.end > s.start);
  const markers = events.filter((e) => e.kind in MARKERS);
  const lanes = STATIONS.filter((s) => runs.some((r) => r.kind === s));

  const track = "relative h-3 rounded-sm bg-muted/50";
  const label = "w-20 shrink-0 font-mono text-xs text-muted-foreground";
  return (
    <div className="space-y-1.5 py-2 pr-2" aria-label="timeline lanes">
      <div className="flex items-center gap-2">
        <span className={label}>state</span>
        <div className={`${track} flex-1`}>
          {segments.map((s, i) => {
            const tone = STATE_STATUS[s.state];
            return (
              <div key={i} className="absolute inset-y-0"
                   title={`${s.state} · ${duration(s.end - s.start)}`}
                   style={{
                     left: x(s.start), width: w(s.start, s.end), minWidth: 2,
                     background: tone ? STATUS[tone] : MUTED,
                     opacity: tone ? 1 : 0.45,
                     // 2px surface gap between adjacent segments.
                     borderRight: `2px solid ${SURFACE[m]}`,
                   }} />
            );
          })}
        </div>
      </div>
      {markers.length > 0 && (
        <div className="flex items-center gap-2">
          <span className={label}>PR</span>
          <div className="relative h-4 flex-1">
            {markers.map((e, i) => {
              const [glyph, what] = MARKERS[e.kind];
              return (
                <span key={i} className="absolute -translate-x-1/2 text-xs leading-4"
                      style={{ left: x(e.ts) }}
                      title={`${what} · ${when(e.ts)}${e.detail ? ` · ${e.detail}` : ""}`}>
                  {glyph}</span>
              );
            })}
          </div>
        </div>
      )}
      {lanes.map((s) => (
        <div key={s} className="flex items-center gap-2">
          <span className={label}>{s}</span>
          <div className={`${track} flex-1`}>
            {runs.filter((r) => r.kind === s).map((r, i) => {
              const failed = runFailed(r.kind, r.detail);
              return (
                <div key={i} className="absolute inset-y-0 rounded-sm"
                     title={`${r.log ?? r.kind} · ${duration((r.duration_ms ?? 0) / 1000)}`
                            + `${r.turns ? ` · ${r.turns} turns` : ""} · ${r.detail ?? ""}`}
                     style={{
                       left: x(startOf(r)), width: w(startOf(r), r.ts), minWidth: 4,
                       background: SERIES[m][STATIONS.indexOf(s)],
                       boxShadow: failed ? `0 0 0 2px ${STATUS.critical}` : undefined,
                     }} />
              );
            })}
          </div>
        </div>
      ))}
      <div className="flex justify-between pl-[5.5rem] text-xs text-muted-foreground tabular-nums">
        <span>{when(t0)}</span>
        <span>{duration(span)}</span>
        <span>{live ? "now" : when(t1)}</span>
      </div>
    </div>
  );
}
