import { useQuery } from "@tanstack/react-query";

export type TaskRow = {
  task_id: string; source: string; task_key: string; title: string | null;
  state: string; repo_key: string | null; branch: string | null;
  pr_number: number | null; pr_url: string | null; attempts: number;
  cost_usd: number; tokens: number; last_error: string | null;
  lease_owner: string | null; lease_until: number; updated_at: number;
  reason?: string; stations?: string[];
  events?: EventRow[];
};

/** Mirrors `AIState.is_terminal`: nothing moves a row out of these on its own. */
export const TERMINAL: readonly string[] = ["done", "blocked", "human-only"];

/** One `events` ledger line. The telemetry columns are 0 / null on lines
 *  written before they existed, and on markers (`state`, `pr_*`). */
export type EventRow = {
  ts: number; task_id: string | null; kind: string; detail: string | null;
  cost_usd: number; tokens: number;
  duration_ms?: number; turns?: number; model?: string | null;
  model_requested?: string | null; log?: string | null;
};

/** One agent run, as `/api/runs` lists it. `open_id` is a row the task sheet
 *  can open -- a triage run's own row (the placeholder) is gone once routed. */
export type RunRow = {
  id: number; ts: number; started: number;
  task_id: string; open_id: string | null;
  task_key: string | null; repo_key: string | null; title: string | null;
  kind: string; detail: string | null; outcome: string; failed: boolean;
  cost_usd: number; tokens: number; duration_ms: number; turns: number;
  model: string | null; models: string[]; model_requested: string | null;
  model_mismatch: boolean; log: string | null;
};

export type FlowStage = "to_pr" | "pr_to_review" | "review_to_merge" | "total" | "waiting";
export type Touch = "clarifications" | "plan_rejections" | "review_rounds"
  | "fix_ci" | "fix_feedback";

/** One merged row. Durations are seconds; null = that stage never happened
 *  (no human review before the merge). `autonomous` null = cannot say. */
export type FlowRow = {
  task_id: string; task_key: string | null; repo_key: string | null;
  title: string | null; pr_url: string | null; pr_number: number | null;
  done_at: number; in_state: Record<string, number>;
  human_commits: number | null; autonomous: boolean | null;
  // Lines changed on the merged PR (additions + deletions), null when GitHub
  // could not say; `size` is its bucket. Cost is the row's own runs.
  lines: number | null; size: string; cost_usd: number; tokens: number;
} & Record<FlowStage, number | null> & Record<Touch, number>;

export type SizeBucket = {
  size: string; n: number; median_cost_usd: number | null;
  median_tokens: number | null; total_cost_usd: number;
};

export type FlowData = {
  rows: FlowRow[];
  summary: {
    merged: number;
    median: Record<FlowStage, number | null>;
    n: Record<FlowStage, number>;
    waiting_share: number | null;
    time_by_state: Record<string, number>;
    autonomy: { known: number; autonomous: number; rate: number | null };
    touches: Record<Touch, number>;
    human_commits: number;
    cost_by_size: SizeBucket[];
  };
};

/** One scorer's verdicts in the window. `rate` is over passed + failed only;
 *  `low`/`high` are its 95% Wilson interval, null with no scored runs. */
export type ScoreCard = {
  scorer: string; stations: string[];
  n: number; passed: number; failed: number;
  insufficient: number; errors: number; pending: number;
  rate: number | null; low: number | null; high: number | null;
  cost_usd: number; tokens: number; labels: Record<string, number>;
  trend: { day: string; passed: number; failed: number }[];
};

export type ScoreResult = {
  id: number; event_id: number; run_ts: number; scorer: string;
  station: string | null; log: string | null;
  label: string | null; score: number | null; passed: boolean | null;
  reasoning: string | null;
  task_id: string; open_id: string | null; task_key: string | null;
  repo_key: string | null; title: string | null;
  cost_usd: number; tokens: number; model: string | null;
};

export type ConfiguredScorer = {
  name: string; stations: string[]; labels: Record<string, number>;
  pass_score: number; sample_rate: number; repos: string[];
};

export type ScoresData = {
  cards: ScoreCard[]; results: ScoreResult[];
  configured: ConfiguredScorer[]; errors: string[]; scorers_dir: string;
};

export type Overview = {
  active: number; max_active_tasks: number;
  today: { cost_usd: number; tokens: number };
  daily_budget_usd: number; daily_tokens: number;
  stuck: number; last_event_ts: number;
  runner_id: string; mode: string;
  // `windows` and `days` together are Pipeline.tick's gate. The last-tick
  // indicator needs both: a gap only means the runner is late if the runner
  // was supposed to be running.
  windows: Record<string, string[]>;
  days: string[];
};

/** One record out of an agent's JSONL run log. Shape varies by `type`. */
export type LogLine = { type?: string; text?: string; [k: string]: unknown };

export type LogPage = {
  lines: LogLine[]; next_offset: number; eof: boolean; missing: boolean;
};

export type SpendDay = {
  day: string; cost_usd: number; tokens: number;
  kinds: Record<string, { cost_usd: number; tokens: number }>;
};

export type CostRow = {
  task_key: string; source: string; repo_key: string | null;
  cost_usd: number; tokens: number;
};

export type SpendData = { days: SpendDay[]; by_task: CostRow[] };

export type ThroughputDay = {
  day: string; pr_open: number; done: number; blocked: number;
};

export type ThroughputData = {
  days: ThroughputDay[];
  cycle_times: { task_id: string; seconds: number }[];
  review_cycles: { task_id: string; cycles: number }[];
};

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    // FastAPI puts the only useful part of a failure in {"detail": ...}.
    // Throwing the status alone leaves the error branch with "500 Internal
    // Server Error" and nothing to act on -- and `serve` raises real
    // sentences there (a missing database names its path).
    let detail = "";
    try {
      const body: unknown = await res.json();
      const d = (body as { detail?: unknown } | null)?.detail;
      if (typeof d === "string") detail = d;
      else if (d != null) detail = JSON.stringify(d);
    } catch {
      // Not JSON -- a proxy error page, or an empty body. The status is all
      // there is, and that is what the caller gets.
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`);
  }
  return res.json() as Promise<T>;
}

// Row ids contain '#'. encodeURIComponent is not optional: an unencoded id is
// truncated at the '#' by the browser before the request leaves, and every
// routed row collapses onto its placeholder.
const qid = (id: string) => encodeURIComponent(id);

export const useOverview = () =>
  useQuery({ queryKey: ["overview"], queryFn: () => get<Overview>("/api/overview"),
             refetchInterval: 5000 });

export const useTasks = () =>
  useQuery({ queryKey: ["tasks"],
             queryFn: () => get<{ tasks: TaskRow[]; stuck: TaskRow[] }>("/api/tasks"),
             refetchInterval: 5000 });

export const useTask = (id: string | null) =>
  useQuery({ queryKey: ["task", id], enabled: !!id,
             queryFn: () => get<TaskRow>(`/api/task?id=${qid(id!)}`),
             refetchInterval: 5000 });

export const useTaskLog = (id: string | null, station: string | null, offset: number) =>
  useQuery({
    queryKey: ["log", id, station, offset], enabled: !!id && !!station,
    queryFn: () => get<LogPage>(
      `/api/task/log?id=${qid(id!)}&station=${encodeURIComponent(station!)}&offset=${offset}`),
    refetchInterval: 2000,
  });

export const useSpend = (days = 30) =>
  useQuery({ queryKey: ["spend", days],
             queryFn: () => get<SpendData>(`/api/spend?days=${days}`),
             refetchInterval: 15000 });

export const useThroughput = (days = 30) =>
  useQuery({ queryKey: ["throughput", days],
             queryFn: () => get<ThroughputData>(`/api/throughput?days=${days}`),
             refetchInterval: 15000 });

// The whole window, filtered client-side: the per-station summary has to
// see every station even while the table shows one.
export const RUNS_LIMIT = 2000;

export const useRuns = (days = 30) =>
  useQuery({ queryKey: ["runs", days],
             queryFn: () => get<{ runs: RunRow[] }>(
               `/api/runs?days=${days}&limit=${RUNS_LIMIT}`),
             refetchInterval: 15000 });

export const useFlow = (days = 30) =>
  useQuery({ queryKey: ["flow", days],
             queryFn: () => get<FlowData>(`/api/flow?days=${days}`),
             refetchInterval: 30000 });

export const useScores = (days = 30) =>
  useQuery({ queryKey: ["scores", days],
             queryFn: () => get<ScoresData>(`/api/scores?days=${days}`),
             refetchInterval: 30000 });

export type GraphTransition = { from: string; to: string; label: string };

export const useGraph = () =>
  useQuery({ queryKey: ["graph"],
             queryFn: () => get<{ transitions: GraphTransition[]; mermaid: string }>("/api/graph") });
