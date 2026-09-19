import { useQuery } from "@tanstack/react-query";

export type TaskRow = {
  task_id: string; source: string; task_key: string; title: string | null;
  state: string; repo_key: string | null; branch: string | null;
  pr_number: number | null; pr_url: string | null; attempts: number;
  cost_usd: number; tokens: number; last_error: string | null;
  lease_owner: string | null; lease_until: number; updated_at: number;
  reason?: string; stations?: string[];
  events?: { ts: number; kind: string; detail: string; cost_usd: number; tokens: number }[];
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

export type GraphTransition = { from: string; to: string; label: string };

export const useGraph = () =>
  useQuery({ queryKey: ["graph"],
             queryFn: () => get<{ transitions: GraphTransition[]; mermaid: string }>("/api/graph") });
