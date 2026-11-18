import type {
  ArrivalRow,
  CacheRow,
  Comparison,
  ConnectorRow,
  EdgeRevision,
  ExposureEstimateDetail,
  ExposureEstimateListItem,
  ExposureEstimatePayload,
  ExposureSources,
  GridRow,
  NodeRow,
  OpenWindow,
  RoadEventRow,
  ScenarioCreatePayload,
  ScenarioListRow,
  ScenarioResult,
  ShareRow,
  ShelterRow,
  SourceDoc,
  Summary,
  TaskRow,
} from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>("/health"),

  createScenario: (payload: ScenarioCreatePayload) =>
    request<ScenarioResult>("/scenarios", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listScenarios: () =>
    request<{ scenarios: ScenarioListRow[] }>("/scenarios"),

  taskSummary: (taskId: string) =>
    request<{ task: TaskRow; summary: Summary | null }>(
      `/tasks/${taskId}/summary`
    ),

  taskGrids: (taskId: string) =>
    request<{ shares: ShareRow[] }>(`/tasks/${taskId}/grids`),

  taskArrivals: (taskId: string) =>
    request<{ arrivals: ArrivalRow[] }>(`/tasks/${taskId}/arrivals`),

  taskWitness: (taskId: string, gridId: string) =>
    request<{
      grid_id: string;
      shares: ShareRow[];
      connector_arrivals: Array<ArrivalRow | null>;
      task_edge_dependencies: Array<{ task_id: string; edge_id: string }>;
    }>(`/tasks/${taskId}/witness?grid_id=${encodeURIComponent(gridId)}`),

  releaseViewer: (taskId: string, viewerId: string) =>
    request<{
      task_id: string;
      viewer_id: string;
      remaining_viewers: number;
      cancelled: boolean;
      note: string;
    }>(`/tasks/${taskId}/release`, {
      method: "POST",
      body: JSON.stringify({ viewer_id: viewerId }),
    }),

  listTasks: () =>
    request<{ tasks: TaskRow[]; cache_index: CacheRow[] }>("/tasks"),

  documents: () =>
    request<{ documents: SourceDoc[] }>("/sources/documents"),

  boundaries: () =>
    request<{
      boundaries: Array<{
        boundary_id: string;
        name: string;
        geometry: number[][];
      }>;
    }>("/sources/boundaries"),

  nodes: () => request<{ nodes: NodeRow[] }>("/sources/nodes"),

  edges: () => request<{ edge_revisions: EdgeRevision[] }>("/sources/edges"),

  events: (includeAudit = false) =>
    request<{
      events: RoadEventRow[];
      audits: Array<Record<string, unknown>>;
    }>(`/sources/events?include_audit=${includeAudit}`),

  shelters: () =>
    request<{ shelters: ShelterRow[]; open_windows: OpenWindow[] }>(
      "/sources/shelters"
    ),

  grids: (asOf: string) =>
    request<{ grids: GridRow[]; connectors: ConnectorRow[] }>(
      `/sources/grids?as_of=${encodeURIComponent(asOf)}`
    ),

  compare: (taskA: string, taskB: string) =>
    request<Comparison>("/comparisons", {
      method: "POST",
      body: JSON.stringify({ task_a_id: taskA, task_b_id: taskB }),
    }),

  publish: (taskA: string, taskB: string, title: string, exposureEstimateId?: string | null) =>
    request<{
      publication: { publication_id: string; [k: string]: unknown };
      comparison: Comparison;
      input_manifest: Record<string, unknown>;
    }>("/publications", {
      method: "POST",
      body: JSON.stringify({
        task_a_id: taskA,
        task_b_id: taskB,
        title,
        created_by: "analyst-ui",
        exposure_estimate_id: exposureEstimateId ?? null,
      }),
    }),

  publications: () =>
    request<{ publications: Record<string, unknown>[] }>(
      "/publications"
    ),

  createEvent: (payload: Record<string, unknown>) =>
    request<{
      event_id: string;
      invalidation: {
        edge_id: string;
        reason: string;
        affected_task_ids: string[];
        recovery_candidate_task_ids: string[];
        staled_total: number;
      };
    }>("/events", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  invalidate: (edgeId: string, reason: string) =>
    request<Record<string, unknown>>("/cache/invalidate", {
      method: "POST",
      body: JSON.stringify({ edge_id: edgeId, reason }),
    }),

  precompute: (payload: Record<string, unknown>) =>
    request<{ precomputed: ScenarioResult[] }>("/cache/precompute", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  rebuild: (newCutoff: string) =>
    request<Record<string, unknown>>("/cache/rebuild", {
      method: "POST",
      body: JSON.stringify({ new_cutoff: newCutoff }),
    }),

  cacheIndex: () =>
    request<{ cache_index: CacheRow[] }>("/cache/index"),

  networkVersions: () =>
    request<{
      versions: Array<{
        version_id: number;
        cutoff_at: string;
        edge_fingerprint: string;
        event_fingerprint: string;
        note: string | null;
      }>;
    }>("/network-versions"),

  reseed: () => request("/admin/reseed", { method: "POST" }),

  applyC2: () => request("/admin/apply-c2-revision", { method: "POST" }),

  // ------------------------------------------------ 临时暴露人口区间 --------
  exposureSources: () => request<ExposureSources>("/exposure/sources"),

  createExposureEstimate: (payload: ExposureEstimatePayload) =>
    request<ExposureEstimateDetail>("/exposure/estimates", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listExposureEstimates: () =>
    request<{ estimates: ExposureEstimateListItem[] }>("/exposure/estimates"),

  exposureEstimate: (estimateId: string) =>
    request<ExposureEstimateDetail>(`/exposure/estimates/${estimateId}`),

  exposureGridDrilldown: (estimateId: string, gridId: string) =>
    request<{
      period: import("./types").ExposurePeriod;
      evidence: import("./types").ExposureEvidence[];
    }>(
      `/exposure/estimates/${estimateId}/grid/${encodeURIComponent(gridId)}`
    ),

  registerMobile: (payload: Record<string, unknown>) =>
    request<{ sample_id: number; doc_id: number }>("/exposure/mobile-signals", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  registerVenue: (payload: Record<string, unknown>) =>
    request<{ presence_id: number; doc_id: number }>("/exposure/venue-presence", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  registerTransfer: (payload: Record<string, unknown>) =>
    request<{ transfer_id: number; doc_id: number; late_arrival: boolean }>(
      "/exposure/transfers",
      { method: "POST", body: JSON.stringify(payload) }
    ),

  registerOverlapRule: (payload: Record<string, unknown>) =>
    request<{ rule_id: number; doc_id: number }>("/exposure/overlap-rules", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};
