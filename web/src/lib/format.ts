/** Seconds as the two largest units: "42s", "4m 12s", "3h 20m", "2d 4h".
 *
 * Two units, not one: "4m" for a run that took 4m 59s hides most of a minute,
 * and stage durations are only as fine as the tick interval anyway, so a third
 * unit would be false precision. `null` is a stage that never happened, which
 * is not the same as zero.
 */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  const units: [string, number][] = [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]];
  const i = units.findIndex(([, size]) => s >= size);
  if (i === -1) return "0s";
  const [u1, n1] = units[i];
  const head = `${Math.floor(s / n1)}${u1}`;
  const next = units[i + 1];
  if (!next) return head;
  const rest = Math.floor((s % n1) / next[1]);
  return rest ? `${head} ${rest}${next[0]}` : head;
}

export const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${Math.round(v * 100)}%`;

export const when = (ts: number) =>
  new Date(ts * 1000).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

export function median(values: number[]): number | null {
  const xs = [...values].sort((a, b) => a - b);
  if (xs.length === 0) return null;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

/** Mirror of `db._outcome`'s failed flag, for ledger lines the sheet renders
 *  without going through `/api/runs`. */
export function runFailed(kind: string, detail: string | null | undefined): boolean {
  const text = (detail ?? "").trim();
  if (text.startsWith("failed")) return true;
  if (kind === "implement") return text !== "ok";
  if (kind === "fix") {
    const cut = text.indexOf(": ");
    const head = cut === -1 ? "" : text.slice(0, cut);
    const caused = head !== "" && head.split("+").every((p) => p === "ci" || p === "feedback");
    return (caused ? text.slice(cut + 2) : text) !== "ok";
  }
  return false;
}
