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
  counts: Record<string, number>; active: number; max_active_tasks: number;
  today: { cost_usd: number; tokens: number };
  daily_budget_usd: number; daily_tokens: number;
  stuck: number; last_event_ts: number;
  runner_id: string; mode: string;
  windows: Record<string, string[]>;
};

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
    queryFn: () => get<{ lines: any[]; next_offset: number; eof: boolean; missing: boolean }>(
      `/api/task/log?id=${qid(id!)}&station=${encodeURIComponent(station!)}&offset=${offset}`),
    refetchInterval: 2000,
  });

export const useSpend = (days = 30) =>
  useQuery({ queryKey: ["spend", days], queryFn: () => get<any>(`/api/spend?days=${days}`),
             refetchInterval: 15000 });

export const useThroughput = (days = 30) =>
  useQuery({ queryKey: ["throughput", days],
             queryFn: () => get<any>(`/api/throughput?days=${days}`),
             refetchInterval: 15000 });

export type GraphTransition = { from: string; to: string; label: string };

export const useGraph = () =>
  useQuery({ queryKey: ["graph"],
             queryFn: () => get<{ transitions: GraphTransition[]; mermaid: string }>("/api/graph") });
