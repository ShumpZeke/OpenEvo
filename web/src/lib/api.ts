/**
 * Control-plane API client.
 *
 * Every view reads through here. There is deliberately no local fixture, seed
 * or placeholder dataset in this codebase: if the backend has no value, the UI
 * renders "no data" rather than inventing one (SOURCE_OF_TRUTH section 36).
 */

export type Json = Record<string, any>;

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail: unknown;
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      detail = body?.detail ?? body;
      if (typeof detail === "string") message = detail;
      else if (detail && typeof detail === "object") message = JSON.stringify(detail);
    } catch {
      /* non-JSON error body — keep the status line */
    }
    throw new ApiError(res.status, message, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const qs = (params: Record<string, unknown>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

export const api = {
  health: () => req<Json>("/api/health"),
  system: () => req<Json>("/api/system"),
  capabilities: () => req<Json>("/api/control/capabilities"),

  runs: () => req<{ runs: Json[] }>("/api/query/runs"),
  run: (id: string) => req<Json>(`/api/query/runs/${id}`),
  summary: (id: string) => req<Json>(`/api/query/runs/${id}/summary`),

  candidates: (id: string, p: Json = {}) =>
    req<{ candidates: Json[]; total: number }>(
      `/api/query/runs/${id}/candidates${qs(p)}`),
  candidate: (id: string, cid: string) =>
    req<Json>(`/api/query/runs/${id}/candidates/${cid}`),

  lineage: (id: string, limit = 3000) =>
    req<{ nodes: Json[]; edges: Json[]; total: number; truncated: boolean }>(
      `/api/query/runs/${id}/lineage${qs({ limit })}`),

  mapElites: (id: string, p: Json = {}) =>
    req<{ cells: Json[]; dimensions: string[]; max_generation: number | null }>(
      `/api/query/runs/${id}/map-elites${qs(p)}`),

  islands: (id: string) =>
    req<{ islands: Json[]; migrations: Json[] }>(`/api/query/runs/${id}/islands`),

  modelRequests: (id: string, p: Json = {}) =>
    req<{ model_requests: Json[]; total: number }>(
      `/api/query/runs/${id}/model-requests${qs(p)}`),

  evaluations: (id: string, p: Json = {}) =>
    req<{ evaluations: Json[]; total: number }>(
      `/api/query/runs/${id}/evaluations${qs(p)}`),

  checkpoints: (id: string) =>
    req<{ checkpoints: Json[] }>(`/api/query/runs/${id}/checkpoints`),

  events: (id: string, p: Json = {}) =>
    req<{ events: Json[] }>(`/api/query/runs/${id}/events${qs(p)}`),

  resources: (id: string, p: Json = {}) =>
    req<{ series: Record<string, Json[]> }>(`/api/query/runs/${id}/resources${qs(p)}`),

  logs: (id: string, stream = "stdout", lines = 300) =>
    req<{ stream: string; text: string }>(
      `/api/query/runs/${id}/logs${qs({ stream, lines })}`),

  search: (q: string, run_id?: string) =>
    req<{ results: Json[] }>(`/api/query/search${qs({ q, run_id })}`),

  compare: (runIds: string[]) =>
    req<{ runs: Json[] }>(`/api/query/compare${qs({ run_ids: runIds.join(",") })}`),

  providers: () => req<Json>("/api/providers"),
  runDoctor: (probeTools = true) =>
    req<Json>(`/api/providers/doctor${qs({ probe_tools: probeTools })}`, { method: "POST" }),
  forceRoute: (role: string, profile_id: string) =>
    req<Json>("/api/providers/force", {
      method: "POST", body: JSON.stringify({ role, profile_id }),
    }),
  resetCircuit: (id: string) =>
    req<Json>(`/api/providers/${id}/reset-circuit`, { method: "POST" }),

  classic: () => req<Json>("/api/classic"),

  startRun: (body: Json) =>
    req<Json>("/api/control/runs", { method: "POST", body: JSON.stringify(body) }),
  stopRun: (id: string, force = false) =>
    req<Json>(`/api/control/runs/${id}/stop`, {
      method: "POST", body: JSON.stringify({ force, timeout: 30 }),
    }),
  checkpointNow: (id: string) =>
    req<Json>(`/api/control/runs/${id}/checkpoint`, { method: "POST" }),
  resumeRun: (id: string, body: Json = {}) =>
    req<Json>(`/api/control/runs/${id}/resume`, {
      method: "POST", body: JSON.stringify(body),
    }),
  cloneRun: (id: string, body: Json = {}) =>
    req<Json>(`/api/control/runs/${id}/clone`, {
      method: "POST", body: JSON.stringify(body),
    }),
  deleteCheckpoint: (id: string, iteration: number) =>
    req<Json>(`/api/control/runs/${id}/checkpoints/${iteration}`, { method: "DELETE" }),
};

/**
 * Live event stream.
 *
 * The server reports how many events it had to drop for a slow client; we
 * surface that to the caller so views can refetch rather than quietly showing
 * a gap-ridden feed.
 */
export function subscribeEvents(
  runId: string | null,
  onEvents: (events: Json[]) => void,
  onDropped?: (count: number) => void,
  onStatus?: (connected: boolean) => void,
): () => void {
  const url = `/api/stream${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`;
  let src: EventSource | null = null;
  let closed = false;
  let retry = 1000;

  const connect = () => {
    if (closed) return;
    src = new EventSource(url);
    src.onopen = () => {
      retry = 1000;
      onStatus?.(true);
    };
    src.addEventListener("events", (e) => {
      try {
        onEvents(JSON.parse((e as MessageEvent).data));
      } catch {
        /* a malformed frame must not kill the stream */
      }
    });
    src.addEventListener("dropped", (e) => {
      try {
        onDropped?.(JSON.parse((e as MessageEvent).data).count);
      } catch {
        /* ignore */
      }
    });
    src.onerror = () => {
      onStatus?.(false);
      src?.close();
      if (!closed) {
        // Exponential backoff, capped: a control plane restart should reconnect
        // on its own without the operator reloading the page.
        setTimeout(connect, retry);
        retry = Math.min(retry * 2, 15000);
      }
    };
  };

  connect();
  return () => {
    closed = true;
    src?.close();
  };
}
