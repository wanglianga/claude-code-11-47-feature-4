import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import CoverageMap from "./CoverageMap";
import type {
  ArrivalRow,
  EdgeRevision,
  NodeRow,
  ScenarioResult,
  ShareRow,
  ShelterRow,
  Summary,
} from "./types";

const T0 = "2026-07-15T08:00:00+00:00";
const C1 = "2026-07-15T07:30:00+00:00";
const C2 = "2026-07-15T08:45:00+00:00";

export default function ScenarioView({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [form, setForm] = useState({
    name: "临时情景",
    population_as_of: "2026-06-01",
    departure_time: T0,
    max_trip_minutes: 30,
    road_cutoff: C1,
    boundary_id: "" as string,
    viewer_id: `viewer-${Math.floor(1000 + Math.random() * 9000)}`,
  });
  const [nodes, setNodes] = useState<NodeRow[]>([]);
  const [edges, setEdges] = useState<EdgeRevision[]>([]);
  const [shelters, setShelters] = useState<ShelterRow[]>([]);
  const [result, setResult] = useState<ScenarioResult | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [shares, setShares] = useState<ShareRow[]>([]);
  const [arrivals, setArrivals] = useState<ArrivalRow[]>([]);
  const [selectedGrid, setSelectedGrid] = useState<string | null>(null);
  const [witness, setWitness] = useState<{
    shares: ShareRow[];
    deps: string[];
  } | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.nodes().then((r) => setNodes(r.nodes)).catch(() => undefined);
    api.edges().then((r) => setEdges(r.edge_revisions)).catch(() => undefined);
    api.shelters().then((r) => setShelters(r.shelters)).catch(() => undefined);
  }, []);

  const loadTask = async (taskId: string) => {
    const [s, g, a] = await Promise.all([
      api.taskSummary(taskId),
      api.taskGrids(taskId),
      api.taskArrivals(taskId),
    ]);
    setSummary(s.summary);
    setShares(g.shares);
    setArrivals(a.arrivals);
    setWitness(null);
    setSelectedGrid(null);
  };

  const run = async () => {
    setLoading(true);
    try {
      const r = await api.createScenario({
        ...form,
        boundary_id: form.boundary_id || null,
      });
      setResult(r);
      await loadTask(r.task_id);
      pushToast(
        `${r.message}｜证据截止 ${r.evidence_cutoff_at.replace("T", " ").slice(0, 16)}`
      );
    } catch (e) {
      pushToast(String(e), "error");
    } finally {
      setLoading(false);
    }
  };

  const selectGrid = async (gridId: string) => {
    setSelectedGrid(gridId);
    if (!result) return;
    try {
      const w = await api.taskWitness(result.task_id, gridId);
      setWitness({
        shares: w.shares,
        deps: w.task_edge_dependencies.map((d) => d.edge_id),
      });
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const witnessEdgeIds = useMemo(() => {
    const ids = new Set<string>();
    witness?.shares.forEach((s) =>
      (s.witness_edge_path ?? []).forEach((e) => ids.add(e))
    );
    return Array.from(ids);
  }, [witness]);

  const witnessNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const a of arrivals) {
      if (witnessEdgeIds.some((e) => a.witness_edge_path.includes(e))) {
        a.witness_node_path.forEach((n) => ids.add(n));
      }
    }
    if (selectedGrid) {
      witness?.shares.forEach((s) => s.connector_node && ids.add(s.connector_node));
    }
    return Array.from(ids);
  }, [arrivals, witness, witnessEdgeIds, selectedGrid]);

  const statusCount = useMemo(() => {
    const c = { covered: 0, uncovered: 0, unknown: 0 };
    const seen = new Set<string>();
    for (const s of shares) {
      if (seen.has(s.grid_id + s.status)) continue;
      seen.add(s.grid_id + s.status);
      c[s.status] += 1;
    }
    return c;
  }, [shares]);

  return (
    <>
      <div className="sidebar">
        <div className="card">
          <h3>情景工作区（固定四项输入）</h3>
          <label className="field">
            <span>情景名称</span>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <label className="field">
            <span>人口基准日</span>
            <input
              type="date"
              value={form.population_as_of}
              onChange={(e) => setForm({ ...form, population_as_of: e.target.value })}
            />
          </label>
          <label className="field">
            <span>出发时刻（UTC）</span>
            <input
              type="datetime-local"
              value={form.departure_time.slice(0, 16)}
              onChange={(e) =>
                setForm({ ...form, departure_time: e.target.value + ":00+00:00" })
              }
            />
          </label>
          <div className="row">
            <label className="field">
              <span>允许行程时长（分钟）</span>
              <input
                type="number"
                value={form.max_trip_minutes}
                onChange={(e) =>
                  setForm({ ...form, max_trip_minutes: Number(e.target.value) })
                }
              />
            </label>
            <label className="field">
              <span>分析边界</span>
              <select
                value={form.boundary_id}
                onChange={(e) => setForm({ ...form, boundary_id: e.target.value })}
              >
                <option value="">全部边界</option>
                <option value="B_MAIN">B_MAIN 主城区</option>
                <option value="B_EXT">B_EXT 扩展区</option>
              </select>
            </label>
          </div>
          <label className="field">
            <span>道路资料截止点</span>
            <select
              value={form.road_cutoff}
              onChange={(e) => setForm({ ...form, road_cutoff: e.target.value })}
            >
              <option value={C1}>C1 07:30（首发管制）</option>
              <option value={C2}>C2 08:45（E7 重开 + E2 新封路，需先在缓存页应用修订）</option>
            </select>
          </label>
          <label className="field">
            <span>查看者标识（引用计数用）</span>
            <input
              value={form.viewer_id}
              onChange={(e) => setForm({ ...form, viewer_id: e.target.value })}
            />
          </label>
          <button className="btn" onClick={run} disabled={loading}>
            {loading ? "计算中…" : "按时变路网求解并覆盖分析"}
          </button>
        </div>

        {result && (
          <div className="card">
            <h3>覆盖汇总（网格级去重后）</h3>
            <div className="evidence">
              证据截止时间：{result.evidence_cutoff_at.replace("T", " ").slice(0, 19)}
              <br />
              缓存：{result.cache_hit ? "命中（签名完全一致）" : "未命中，重新计算"}｜
              {result.message}
            </div>
            {summary && (
              <div className="stat-grid">
                <div className="stat"><div className="v">{summary.total_population}</div><div className="k">边界内总人口（分母）</div></div>
                <div className="stat"><div className="v">{summary.located_population}</div><div className="k">可定位人口</div></div>
                <div className="stat good"><div className="v">{summary.covered_population}</div><div className="k">覆盖人口（去重）</div></div>
                <div className="stat warn"><div className="v">{summary.uncovered_population}</div><div className="k">可定位但未覆盖</div></div>
                <div className="stat"><div className="v" style={{ color: "var(--unknown)" }}>{summary.unknown_population}</div><div className="k">未知（单列）</div></div>
                <div className="stat"><div className="v">{summary.covered_grid_count}</div><div className="k">覆盖网格数</div></div>
              </div>
            )}
            <div className="legend">
              <span><i style={{ background: "var(--covered)" }} />覆盖 {statusCount.covered}</span>
              <span><i style={{ background: "var(--uncovered)" }} />未覆盖 {statusCount.uncovered}</span>
              <span><i style={{ background: "var(--unknown)" }} />未知 {statusCount.unknown}</span>
              <span><i style={{ background: "var(--shelter)" }} />安置点</span>
              <span><i style={{ background: "#e8c33c" }} />见证路径边</span>
            </div>
            <div style={{ marginTop: 8 }}>
              <button
                className="btn secondary"
                onClick={() =>
                  api
                    .releaseViewer(result.task_id, form.viewer_id)
                    .then((r) => pushToast(`查看者已离开：${r.note}`))
                    .catch((e) => pushToast(String(e), "error"))
                }
              >
                离开当前查看（不取消他人任务）
              </button>
            </div>
          </div>
        )}

        {witness && selectedGrid && (
          <div className="card">
            <h3>下钻：{selectedGrid} 的互斥份额与证据</h3>
            <table>
              <thead>
                <tr><th>份额</th><th>连接节点</th><th>人口</th><th>状态/原因</th><th>到发与等待</th></tr>
              </thead>
              <tbody>
                {witness.shares.map((s) => (
                  <tr key={s.share_index}>
                    <td>#{s.share_index}</td>
                    <td className="mono">{s.connector_node || "—"}</td>
                    <td>{s.share_population}</td>
                    <td>
                      <span className={`pill ${s.status}`}>{s.status}</span>
                      {s.shelter_id && <div className="small muted">→ {s.shelter_id}</div>}
                      {s.reason && <div className="small warn">{s.reason}</div>}
                    </td>
                    <td className="small">
                      {s.departure_time ? (
                        <>
                          发 {s.departure_time.replace("T", " ").slice(11, 16)}
                          <br />达 {s.arrival_time?.replace("T", " ").slice(11, 16)}
                          <br />等待 {Math.round(s.total_wait_sec)}s
                          <div>
                            {(s.witness_edge_path ?? []).map((e) => (
                              <span key={e} className="path-chip hl">{e}</span>
                            ))}
                          </div>
                        </>
                      ) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="small muted" style={{ marginTop: 6 }}>
              任务依赖边（封路修订沿此定位）：
              <div>{witness.deps.map((e) => <span key={e} className="path-chip">{e}</span>)}</div>
            </div>
          </div>
        )}
      </div>

      <div className="map-pane">
        <CoverageMap
          nodes={nodes}
          edges={edges}
          shelters={shelters}
          shares={shares}
          arrivals={arrivals}
          witnessEdgeIds={witnessEdgeIds}
          witnessNodeIds={witnessNodeIds}
          selectedGridId={selectedGrid}
          onSelectGrid={selectGrid}
        />
        {result && (
          <div className="evidence" style={{ position: "absolute", left: 12, top: 12, maxWidth: 360 }}>
            点击网格下钻份额、到发时刻、见证边与排除原因。所有颜色均由当前任务的计算结果驱动。
          </div>
        )}
      </div>
    </>
  );
}
